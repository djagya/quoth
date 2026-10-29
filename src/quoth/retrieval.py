"""The serving path's one vault retrieval: prefetch's candidate pool and `evidence_bundle`
both rank through `retrieve`.

- **hybrid** (default, `recall_defaults.RETRIEVAL`): reciprocal-rank fusion (RRF, k=60) of
  the dense list (bge-m3 cosine over the admitted snapshot's matrix), the BM25 (unicode61)
  and trigram FTS5 lists (`search.lexical_lists`) and the *named* list: the chunks of notes
  the query names exactly (title, frontmatter alias or vault path, `named_paths`), in
  cosine order. Exact names and paths a dense model blurs are found by the lexical lists.
- **dense**: cosine order alone (the 0.16 ranking), for comparison in evals.

Every hit keeps its real cosine (computed from the matrix for lexical-only hits), so the
relevance gate still judges cosine. Filters (`Filter`: a folder prefix, excluded recall
tiers) apply before ranking, in the matrix and in the FTS query, never after the cut.
Excerpts whose normalized text repeats an earlier hit are dropped, and at most `per_note`
hits of one note come before any repeat (`ranking.diversify`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
import re
import unicodedata

import numpy as np

from . import recall_defaults as D
from .ranking import diversify
from .search import RRF_K, lexical_lists
from .serving_cache import top

_WORD = re.compile(r"\w+", re.UNICODE)


class RetrievalInputError(ValueError):
    """A refused filter or path; `code` is stable and safe to show."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------- filters

@dataclass(frozen=True)
class Filter:
    """What a retrieval may return: notes under `path_prefix` (a vault-relative folder,
    ending in "/"), outside the `exclude_scopes` (the explicit recall tier)."""

    path_prefix: str = ""
    exclude_scopes: frozenset[str] = field(default_factory=frozenset)

    def __bool__(self) -> bool:
        return bool(self.path_prefix or self.exclude_scopes)

    def allows(self, path: str, scope: str) -> bool:
        return path.startswith(self.path_prefix) and scope not in self.exclude_scopes

    def sql(self) -> tuple[str, tuple]:
        """The same rule as an SQL fragment over `s` (sources), for the FTS lists."""
        where, params = "", []
        if self.path_prefix:
            where += " AND substr(s.path, 1, ?) = ?"
            params += [len(self.path_prefix), self.path_prefix]
        if self.exclude_scopes:
            where += f" AND s.scope NOT IN ({','.join('?' * len(self.exclude_scopes))})"
            params += sorted(self.exclude_scopes)
        return where, tuple(params)


def _relative(raw, code: str) -> str:
    if not isinstance(raw, str):
        raise RetrievalInputError(code)
    value = raw.strip()
    parts = value.split("/")
    if (not value or len(value) > 512 or value.startswith("/") or "\\" in value or "\x00" in value
            or any(p in ("..", ".") for p in parts)):
        raise RetrievalInputError(code)
    return value


def folder_prefix(raw) -> str:
    """A validated vault-relative folder prefix ("PROJECTS/Garden/"); no `..`, not absolute."""
    value = _relative(raw, "invalid_path_prefix").rstrip("/")
    if not value or "//" in value:
        raise RetrievalInputError("invalid_path_prefix")
    return value + "/"


def note_ref(raw) -> tuple[str, str | None]:
    """(vault-relative note path, heading or None) from "Folder/Note.md#Heading"."""
    if not isinstance(raw, str):
        raise RetrievalInputError("invalid_path")
    path, _, heading = raw.partition("#")
    path = _relative(path, "invalid_path")
    if path.endswith("/") or "//" in path:
        raise RetrievalInputError("invalid_path")
    heading = " ".join(heading.split()) or None
    return path, heading


# ---------------------------------------------------------------- names

def fold(text: str) -> str:
    """Casefolded words joined by single spaces (ё as е): how names are compared."""
    folded = unicodedata.normalize("NFC", str(text or "")).casefold().replace("ё", "е")
    return " ".join(_WORD.findall(folded))


def is_name(folded: str) -> bool:
    """A name specific enough to count as the query naming a note: two or more words, or
    one word of 6+ letters or with a digit (dates). "Plan" or "Notes" alone never name one."""
    words = folded.split()
    if len(words) >= 2:
        return True
    return bool(words) and (len(words[0]) >= 6 or any(ch.isdigit() for ch in words[0]))


def note_names(conn) -> dict[str, frozenset[str]]:
    """{folded name: paths} over current notes: title (file stem), aliases, path with and
    without the .md extension."""
    from .chunker import note_aliases
    out: dict[str, set[str]] = {}
    for path, metadata in conn.execute(
            "SELECT s.path, v.metadata FROM sources s"
            " JOIN source_versions v ON v.source_id = s.id AND v.is_current = 1"):
        stem = PurePosixPath(path).stem
        for name in {stem, *note_aliases(metadata), path, path.removesuffix(".md")}:
            folded = fold(name)
            if is_name(folded):
                out.setdefault(folded, set()).add(path)
    return {k: frozenset(v) for k, v in out.items()}


def named_paths(names: dict, query: str) -> set[str]:
    """Paths of the notes whose name appears in `query` as a whole phrase."""
    padded = f" {fold(query)} "
    out: set[str] = set()
    for name, paths in names.items():
        if f" {name} " in padded:
            out |= paths
    return out


def excerpt_key(text: str) -> str:
    """Normalized text: two hits with the same key are the same excerpt."""
    return fold(text)


# ---------------------------------------------------------------- retrieve

def retrieve(st, model: str, qvec: np.ndarray, query: str, k: int, *, mode: str = D.RETRIEVAL,
             filt: Filter = Filter(), per_note: int | None = None) -> list[dict]:
    """Up to `k` vault candidates, best first: ``{chunk_id, cosine, via, named}``.

    `st` is the snapshot's `serving_cache.ConnState`, `qvec` the query vector. `via` names
    the lists that found the hit ("dense", "bm25", "trigram", "named", joined by "+");
    `named` is True when the query names the hit's note.
    """
    if mode not in D.RETRIEVAL_MODES:
        raise RetrievalInputError("invalid_retrieval")
    if k <= 0:
        return []
    ids, mat = st.matrix(model)
    catalog = st.catalog()
    sims = mat @ qvec if ids.size else np.zeros(0, dtype=np.float32)
    mask = st.mask(model, filt)
    depth = max(k, D.HYBRID_DEPTH) if mode == "hybrid" else max(k, 4 * k)
    dense = top(ids, sims, depth, mask) if ids.size else []
    named = named_paths(st.names(), query) if mode == "hybrid" else set()
    if mode == "dense":
        order = [cid for cid, _ in dense]
        lists = {cid: ["dense"] for cid in order}
    else:
        ranked = {"dense": [cid for cid, _ in dense]}
        where, params = filt.sql()
        ranked.update(lexical_lists(st.conn, query, D.HYBRID_DEPTH, where=where, params=params))
        named_ids = [cid for p in named for cid in catalog.by_path.get(p, ()) if filt.allows(p, catalog.where(cid)[1])]
        ranked["named"] = sorted(named_ids, key=lambda c: (-(_cosine(ids, sims, c) or -2.0), c))[:D.HYBRID_DEPTH]
        score: dict[int, float] = {}
        lists: dict[int, list[str]] = {}
        for name, chunk_ids in ranked.items():
            for rank, cid in enumerate(chunk_ids, 1):
                score[cid] = score.get(cid, 0.0) + 1.0 / (RRF_K + rank)
                lists.setdefault(cid, []).append(name)
        order = sorted(score, key=lambda c: (-score[c], c))
    # Same excerpt at another path (a copy, a template): keep the first only.
    order = _distinct(st.conn, order)
    if per_note is not None:
        order = [cid for (cid,) in diversify([(c,) for c in order], {c: catalog.where(c)[0] for c in order},
                                             k, per_source=per_note)]
    out = []
    for cid in order[:k]:
        path = catalog.where(cid)[0]
        out.append({"chunk_id": cid, "cosine": _cosine(ids, sims, cid), "via": "+".join(lists[cid]),
                    "named": path in named})
    return out


def _cosine(ids: np.ndarray, sims: np.ndarray, chunk_id: int) -> float | None:
    """The chunk's cosine from the matrix (ids are sorted), None when it has no vector."""
    i = int(np.searchsorted(ids, chunk_id))
    if i < ids.size and int(ids[i]) == chunk_id:
        return float(sims[i])
    return None


def _distinct(conn, order: list[int]) -> list[int]:
    if not order:
        return []
    texts = dict(conn.execute(f"SELECT id, text FROM chunks WHERE id IN ({','.join('?' * len(order))})", order))
    seen, out = set(), []
    for cid in order:
        key = excerpt_key(texts.get(cid, ""))
        if key and key in seen:
            continue
        seen.add(key)
        out.append(cid)
    return out
