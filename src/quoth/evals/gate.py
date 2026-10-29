"""The nightly gate's and daily quality's scorer, on the harness (see docs/EVAL.md).

Both score a frozen snapshot's vault retrieval for the rows of an eval set (``--gate-set``,
``--quality-set``) and the owner's private v2 set. Retrieval is the harness's: one dry-run
``prefetch`` per row on an in-process sidecar over that snapshot (`InProcessTarget`), whose
vault candidates are the served dense top-10 evidence bundle. Per-row ranks come from
`metrics.observe`, the same matching `eval run` uses; the gate's rules (tolerances, lost
gold, excused items, gold drift) stay in `nightly.py` and `gold_drift.py`.

Nothing here returns or persists question text, paths or excerpts: results hold row ids
and aggregates only (the gate-only ``gold`` paths are stripped before any receipt).
"""
from __future__ import annotations

from pathlib import Path
import time

from . import metrics as M
from .format import EvalSet, load_set, vault_paths
from ..gold_drift import canonical_hits
from .run import InProcessTarget, RunError

GATE_K = 10
# The gate waits for every evidence call: a slow row must never score as a miss. The
# nightly deadline (runtime_budget) still bounds the whole run.
GATE_EVIDENCE_BUDGET = 900.0
# Rows the vault gate never judges: legacy adversarial rows and unanswerables.
SKIP_KINDS = frozenset(("adversarial", "unanswerable"))
REPOSITORY_SOURCE_V3 = "private-golden-v3"
CODE_FILES = ("evals/gate.py", "evals/run.py", "evals/metrics.py", "evals/format.py", "sidecar.py",
              "evidence_service.py", "dated.py", "serving_cache.py", "gold_drift.py")


def as_set(value) -> EvalSet:
    """An `EvalSet` as is, or an eval set v1 file loaded and validated."""
    if isinstance(value, EvalSet):
        return value
    return load_set(value)


def input_path(value) -> Path | None:
    """The file a set came from (for input hashes), whatever form it was passed in."""
    if isinstance(value, EvalSet):
        return value.path
    return Path(value) if value is not None else None


def legacy_kind(row: dict) -> str:
    return next((t.split(":", 1)[1] for t in row.get("tags", []) if t.startswith("legacy_kind:")),
                row["kind"])


def gate_rows(evalset: EvalSet) -> list[dict]:
    """The gate's view of a set: id, question, legacy kind, expected vault paths, abstain."""
    return [{"id": r["id"], "question": r["question"], "kind": legacy_kind(r),
             "expected_paths": vault_paths(r), "abstain": bool(r["expect"]["abstain"]),
             "origin": r["origin"]} for r in evalset.rows]


def gold_missing(rows: list[dict]) -> bool:
    """A row that should have vault gold but has none (golden-v1 rows with empty paths)."""
    return any(r.get("abstain") and not r["expected_paths"] and r["kind"] not in SKIP_KINDS
               for r in rows)


def judged(row: dict) -> bool:
    """Scored by the vault gate: has vault gold and is not adversarial/unanswerable."""
    return bool(row["expected_paths"]) and row.get("kind") not in SKIP_KINDS


def path_scores(bundle: list[dict], expected) -> dict:
    """hit@5/hit@10, MRR@10 and recall@5/@10 of one row (`metrics.observe` vault ranks)."""
    expected = sorted(set(expected))
    row = {"id": "row", "kind": "fact", "origin": "authored", "lang": "en",
           "expect": {"abstain": False, "sources": []}}
    sources = [{"store": "vault", "match": {"path": p}} for p in expected]
    ranks = M.observe(row, {"vault": bundle}, sources=sources)["stores"]["vault"]["ranks"]
    found = [r for r in ranks if r is not None and r <= GATE_K]
    return {"hit@5": int(any(r <= 5 for r in found)), "hit@10": int(bool(found)),
            "mrr@10": M._rr(min(found, default=None)),
            "recall@5": sum(r <= 5 for r in found) / len(expected), "recall@10": len(found) / len(expected)}


def score_dataset(scorer, questions, original_paths, aliases, identities, *, adapter=None,
                  keep_abstain=False, latency=None):
    """Per-row path scores on one snapshot; returns (scored rows, bundle sizes of abstain rows).

    ``adapter`` (the private v2 runner) supplies hit@10/MRR@10 for its own set, as before;
    every other set is scored by the harness metrics. Bundles are canonicalized with this
    row"s gold aliases (``identities`` only for the snapshot gold was resolved against).
    """
    scored, abstain = [], []
    for q in questions:
        if not judged(q):
            if keep_abstain and q.get("abstain"):
                bundle, ms = scorer.bundle(q["question"])
                if latency is not None:
                    latency.append(ms)
                abstain.append((q["id"], q.get("origin"), len(bundle)))
            continue
        bundle, ms = scorer.bundle(q["question"])
        if latency is not None:
            latency.append(ms)
        row_aliases = {p: aliases[p] for p in original_paths[q["id"]] if p in aliases}
        bundle = canonical_hits(bundle, row_aliases, identities)
        expected = set(q["expected_paths"])
        score = path_scores(bundle, expected)
        if adapter is not None:
            legacy = adapter.metrics(bundle[:5], bundle, expected)
            score.update({"hit@10": legacy["hit@10"], "mrr@10": legacy["mrr@10"]})
        scored.append({"id": q["id"], "origin": q.get("origin"), **score,
                       "gold": sorted(set(original_paths[q["id"]]))})
    return scored, abstain


class SnapshotScorer:
    """Vault candidates of one frozen snapshot, through the harness's in-process sidecar.

    An empty in-memory ledger stands in for claims (the gate scores the vault store only);
    `embedder` is the caller's loaded model, shared by every snapshot of a run.
    """

    def __init__(self, db: Path, *, embedder=None, model: str = "bge-m3",
                 evidence_budget: float = GATE_EVIDENCE_BUDGET):
        self.target = InProcessTarget.build(db=db, ledger=":memory:", model=model, embedder=embedder,
                                            evidence_budget=evidence_budget)
        try:
            self.target.prepare()
        except BaseException:
            self.target.close()
            raise

    def bundle(self, question: str) -> tuple[list[dict], float | None]:
        """(top-10 vault candidates as {path, locator, score}, evidence milliseconds).

        The latency is the sidecar's own vault stage (the evidence call), else the wall
        time of the whole dry-run prefetch.
        """
        started = time.monotonic()
        out = self.target.prefetch({"query": question, "dry_run": True, "k_candidates": GATE_K})
        wall = (time.monotonic() - started) * 1000
        detail = out.get("detail") or {}
        if (detail.get("status") or {}).get("vault") != "ok":
            raise RunError("gate_vault_unavailable")
        items = [{"path": c.get("path"), "locator": c.get("locator"), "score": c.get("score")}
                 for c in (detail.get("candidates") or {}).get("vault", [])][:GATE_K]
        if any(not isinstance(i["path"], str) for i in items):
            raise RunError("gate_vault_candidate_invalid")
        ms = (detail.get("timings_ms") or {}).get("vault")
        return items, ms if isinstance(ms, (int, float)) else wall

    def close(self) -> None:
        future = getattr(self.target.app, "_evidence_future", None)
        if future is not None and not future.done():
            # Interrupted mid-row (the nightly deadline): closing the snapshot would wait for
            # the in-flight evidence call under its lease; leave that connection to the GC.
            self.target.app.close()
            return
        self.target.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def quality_set_summary(observations: list[dict], *, dataset_sha256: str,
                        repository_source: str) -> dict:
    """The daily ``golden_v3`` block (quoth-serving-quality/v1): path retrieval only.

    ``observations`` are ``{id, origin, metrics | None (abstain), returned}`` per scored row
    (``returned``: vault candidates of an abstain row, for abstain_with_nonempty_bundle@10).
    """

    def aggregate(group):
        answerable = [o for o in group if o["metrics"] is not None]
        abstain = [o for o in group if o["metrics"] is None]
        if not group or not answerable:
            raise ValueError("quality_golden_v3_empty")
        out = {"questions": len(group), "n": len(answerable), "judged_non_abstain": len(answerable),
               "abstain": len(abstain),
               "abstain_with_nonempty_bundle@10": sum(o["returned"] > 0 for o in abstain)}
        for metric in ("hit@5", "hit@10", "mrr@10", "recall@10"):
            out[metric] = sum(o["metrics"][metric] for o in answerable) / len(answerable)
        return out

    result = aggregate(observations)
    origins = sorted({o["origin"] for o in observations})
    result.update({
        "dataset_sha256": dataset_sha256,
        "repository_source": repository_source,
        "measurement_kind": "source_path_retrieval",
        "answer_level_accuracy": "not_measured",
        "items": {o["id"]: bool(o["metrics"] and o["metrics"]["hit@10"]) for o in observations},
        "per_origin": {origin: aggregate([o for o in observations if o["origin"] == origin])
                       for origin in origins},
    })
    return result


def quality_set_identity(evalset: EvalSet) -> tuple[str, str]:
    """(dataset_sha256, repository_source) for the receipt.

    A set converted from golden-v3 keeps reporting the original file's sha256 and the
    private golden-v3 source name, so receipts stay comparable across the flag switch.
    """
    source = evalset.manifest.get("source") or {}
    if source.get("format") == "golden-v3" and isinstance(source.get("sha256"), str):
        return source["sha256"], REPOSITORY_SOURCE_V3
    return evalset.sha256, "quoth-evalset/v1"
