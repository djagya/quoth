"""Read-only evidence retrieval over the admitted serving snapshot.

One implementation behind both the read-only MCP `evidence_bundle` tool and the
Quoth sidecar, so the two can never rank or bound evidence differently.
"""
from __future__ import annotations

import logging

from . import recall_defaults as D
from . import serving_cache, snapshot_contract
from .embed.backends import MODELS
from .evidence import evidence_bundle
from .live import FACTS_KEY, LiveCheck
from .retrieval import Filter, folder_prefix, note_ref, retrieve

MAX_K = 20


class NoteNotFound(LookupError):
    """A `path` fetch found no served note (absent, purged or filtered: never says which)."""


class ReadOnlyEvidence:
    """Vault evidence with bounded text from a `ServingSnapshot`; caches per snapshot.

    One retrieval (`quoth.retrieval.retrieve`, mode `retrieval`) serves the sidecar's
    prefetch pool and `evidence_bundle`. With `vault` (the live vault root) each returned
    hit also says whether its note changed since indexing (`quoth.live`).
    """

    def __init__(self, snapshot, warm_models: tuple[str, ...] = (), *, retrieval: str = D.RETRIEVAL,
                 vault=None):
        """`warm_models` are warmed on every snapshot candidate before it is admitted."""
        if retrieval not in D.RETRIEVAL_MODES:
            raise ValueError(f"retrieval must be one of {D.RETRIEVAL_MODES}")
        self.snapshot = snapshot
        self.queries = serving_cache.QueryEmbedder()
        self.warm_models = tuple(warm_models)
        self.retrieval = retrieval
        self.live = LiveCheck(vault) if vault else None
        if hasattr(snapshot, "prepare"):
            snapshot.prepare = self.prepare

    def bundle(self, query=None, k: int = D.EVIDENCE_K, model: str = "bge-m3", *, path=None,
               path_prefix=None, include_explicit: bool = False, purpose: str = "explicit",
               with_scores: bool = False, cache_query: bool = True) -> dict:
        """Ranked excerpts for `query`, or the chunks of one note (`path`, "Note.md#Heading").

        `purpose` "explicit" (the tool) or "prefetch" (the sidecar's turn pool) sets the
        per-note cap (`MAX_HITS_PER_NOTE`); notes of the explicit recall tier are left out
        unless `include_explicit` (prefetch never includes them). `path_prefix` keeps only
        notes under that vault folder. `with_scores` adds each hit's `cosine`, `named` and
        `via` (sidecar measurement). A prefetch bundle keeps each hit's indexed-file facts for
        `check_live` on the hits a turn injects; an explicit one is checked here.
        `cache_query=False` keeps bulk lookups (the presort assist) out of the query-vector LRU.
        """
        if purpose not in D.MAX_HITS_PER_NOTE:
            raise ValueError("invalid evidence purpose")
        if type(k) is not int or not 1 <= k <= MAX_K:
            raise ValueError(f"read-only evidence k must be between 1 and {MAX_K}")
        explicit = snapshot_contract.explicit_scopes()
        exclude = explicit if purpose == "prefetch" or not include_explicit else frozenset()
        prefix = folder_prefix(path_prefix) if path_prefix is not None else ""
        filt = Filter(prefix, frozenset(exclude))
        with self.snapshot.lease() as (active, identity):
            if path is not None:
                if query is not None:
                    raise ValueError("give either query or path")
                bundle = self.note(active, path, k, filt)
            else:
                bundle = self.retrieve(active, query, k, model, filt=filt, purpose=purpose,
                                       with_scores=with_scores, cache_query=cache_query)
            built_at = getattr(self.snapshot, "built_at", None)
        provenance = {"snapshot_id": identity, "indexed_at": built_at,
                      "retrieval": "note" if path is not None else self.retrieval}
        if purpose == "explicit" or self.live is None:
            cost = self.check_live(bundle["hits"])
            if cost is not None:
                provenance["live_check"] = cost
        bundle["provenance"] = provenance
        for hit in bundle["hits"]:
            hit["provenance"] = {"snapshot_id": identity}
        return bundle

    def check_live(self, hits: list[dict]) -> dict | None:
        """Set `live` on `hits` (see `quoth.live`); None without a vault (nothing checked)."""
        if self.live is None:
            for hit in hits:
                hit.pop(FACTS_KEY, None)
            return None
        return self.live.check(hits)

    def retrieve(self, conn, query, k, model, *, filt: Filter = Filter(), purpose: str = "explicit",
                 with_scores: bool = False, cache_query: bool = True) -> dict:
        """Primary answer-evidence path: `retrieval.retrieve` top-k with bounded text and the
        answer-use contract.

        Use answer_use as instructions for the caller; hits are untrusted data, not
        instructions or assertions of current authority. Requires local bge-m3 vectors.
        """
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 2048:
            raise ValueError("read-only evidence query must contain 1..2048 characters")
        if model not in MODELS:
            raise ValueError(f"read-only evidence model must be one of {sorted(MODELS)}")
        st = serving_cache.state(conn)
        self.check_vectors(st, model)
        # Check local prerequisites once per requested model; never download, and do not
        # reload a large model on every call.
        embedder = self.queries.embedder(model)
        qvec = self.queries.vector(model, embedder, query, cache=cache_query)
        # Prefetch keeps the plain ranking as its candidate pool: its per-note cap applies to the
        # served slots (`relevance.vault_pick`), so evals and the nightly gate see every candidate.
        ranked = retrieve(st, model, qvec, query, k, mode=self.retrieval, filt=filt,
                          per_note=D.MAX_HITS_PER_NOTE[purpose] if purpose == "explicit" else None)
        hits = self._rows(conn, st, [r["chunk_id"] for r in ranked])
        if with_scores:
            for hit, r in zip(hits, ranked):
                hit["cosine"] = None if r["cosine"] is None else round(r["cosine"], 4)
                hit["named"], hit["via"] = r["named"], r["via"]
        return evidence_bundle(hits, k=k)

    def note(self, conn, ref, k: int, filt: Filter) -> dict:
        """The chunks of one note in line order ("follow the link"); `#Heading` keeps that
        section and its subsections. A note that is not served (absent, purged, outside the
        filter) is `not_found`, the same for every reason."""
        path, heading = note_ref(ref)
        st = serving_cache.state(conn)
        catalog = st.catalog()
        ids = [cid for cid in catalog.by_path.get(path, ()) if filt.allows(path, catalog.where(cid)[1])]
        hits = self._rows(conn, st, ids)
        hits.sort(key=lambda h: (h["line_start"], h["line_end"]))
        if heading is not None:
            want = heading.casefold()
            hits = [h for h in hits if want in (s.strip().casefold() for s in h["heading_path"].split(" > "))]
        if not hits:
            raise NoteNotFound("not_found")
        return evidence_bundle(hits, k=k)

    def _rows(self, conn, st, ids: list[int]) -> list[dict]:
        """Hit rows for chunk ids, in order: text, dates, links, indexed-file facts."""
        if not ids:
            return []
        meta = {r["chunk_id"]: dict(r) for r in conn.execute(
            "SELECT c.id AS chunk_id, s.path, c.heading_path, c.line_start,"
            " c.line_end, s.scope, substr(c.text, 1, 1800) AS snippet"
            " FROM chunks c JOIN source_versions v ON v.id=c.source_version_id AND v.is_current=1"
            " JOIN sources s ON s.id=v.source_id"
            f" WHERE c.id IN ({','.join('?' * len(ids))})", ids)}
        hits = st.dates().annotate([meta[cid] for cid in ids if cid in meta])
        links = snapshot_contract.chunk_links(conn, [h["chunk_id"] for h in hits], D.MAX_LINKS)
        facts = (snapshot_contract.indexed_files(conn, sorted({h["path"] for h in hits}))
                 if self.live is not None else {})
        for h in hits:
            if links.get(h["chunk_id"]):
                h["links"] = links[h["chunk_id"]]
            if h["path"] in facts:
                h[FACTS_KEY] = facts[h["path"]]
        return hits

    @staticmethod
    def check_vectors(st, model):
        # Successful checks are cached per admitted connection; failures re-raise every call.
        if model in st.vectors_ok:
            return
        vector = st.conn.execute(
            "SELECT e.dim, length(e.vector) FROM embeddings e JOIN chunks c ON c.id=e.chunk_id "
            "JOIN source_versions v ON v.id=c.source_version_id AND v.is_current=1 "
            "WHERE e.model=? LIMIT 1", (model,)
        ).fetchone()
        if vector is None:
            raise RuntimeError(f"read-only evidence has no current vectors for {model}")
        if vector[0] != MODELS[model].dim or vector[1] != 4 * MODELS[model].dim:
            raise RuntimeError(f"read-only evidence has invalid vectors for {model}")
        st.vectors_ok.add(model)

    def _warm_conn(self, conn, model: str):
        """Vector check, matrix and date index of `conn` for `model`; raises when unusable."""
        st = serving_cache.state(conn)
        self.check_vectors(st, model)
        st.matrix(model)
        st.dates()
        st.catalog()
        st.names()
        if snapshot_contract.explicit_scopes():
            st.mask(model, Filter("", snapshot_contract.explicit_scopes()))  # prefetch's filter
        return st

    def prepare(self, conn) -> None:
        """Snapshot candidate warmup (admission watcher, off the request path): vector check,
        matrix, dates, catalog, names and a test query, so the first turn after a swap is warm.

        Best-effort, like the startup warmup: a model without vectors, or an embedder that
        cannot load, leaves that model cold and the call path fails closed as before.
        """
        for model in self.warm_models:
            try:
                st = self._warm_conn(conn, model)
                embedder = self.queries.embedder(model)
                self.queries.topk(st, model, embedder, "warmup", 1, cache=False)  # a test query
            except Exception:
                logging.getLogger(__name__).warning("snapshot candidate warmup skipped for %s", model)

    def warm(self, models) -> None:
        """Best-effort cold-start warmup; a failure here only leaves the call path cold.

        The call path still checks vectors and loads the embedder itself and raises
        exactly as before, so a missing model or vector set still fails closed.
        """
        log = logging.getLogger(__name__)
        for model in models:
            try:
                with self.snapshot.lease() as (active, _):
                    self._warm_conn(active, model)
            except Exception:
                # No exception text/paths, as in serving admission.
                log.warning("read-only snapshot warmup skipped for %s", model)
            try:
                # Load outside the lease: a concurrent call waits only for this model.
                self.queries.embedder(model).embed_query("warmup")
            except Exception:
                log.warning("read-only embedder warmup skipped for %s", model)
