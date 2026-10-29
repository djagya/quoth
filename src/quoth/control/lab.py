"""Fixed, fail-closed offline lab evaluation; aggregate receipts only.

Evaluation does not activate caller policy or confer a serving verdict. Candidate
export requires *every* declared gate, including protected item equality.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from time import monotonic

from ..assets import eval_dir
from ..evals.gate import path_scores
from ..pipeline import run, spec_hash, validate
from .lab_suites import baseline_caller, facet_results, score_item, summarize as temporal_summary
from .lab_suites import validate as validate_gold

SUITES = ("ordinary", "protected_facets", "e1_temporal", "abstention")
GATE_NAMES = {"G1_temporal_answer", "G2_seed", "G3_abstention", "G4_ordinary_no_regression",
              "G5_authority_provenance", "G6_latency", "G7_bounds", "Q2_protected_facets",
              "ordinary_no_regression"}
ASSETS = eval_dir()


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _mean(rows, key):
    vals = [r[key] for r in rows if r is not None]
    return round(sum(vals) / len(vals), 6) if vals else None


def path_metrics(hits, expected):
    """recall@5/@10, hit@5/@10 and MRR@10 of one row with the harness metrics; None when
    the row has no gold (unjudged, never a correct abstention)."""
    if not expected:
        return None
    scores = path_scores(hits, expected)
    return {key: float(scores[key]) for key in ('recall@5', 'recall@10', 'hit@5', 'hit@10', 'mrr@10')}


def _safe_rows(rows):
    return [{"id": r["id"], "pass": r["pass"]} for r in rows]


def _score(conn, spec, questions, embedder, snapshot_id, digest, *, temporal=False, cancelled=lambda: False):
    out = []
    for item in questions:
        if cancelled():
            raise TimeoutError("evaluation_budget_exceeded")
        start = monotonic()
        _, bundle, private = run(conn, spec, item["question"], snapshot_id=snapshot_id,
                                 db_sha256=digest, embedder=embedder, k=15 if temporal else 10)
        # The private rows are never returned, serialized, or retained in an eval receipt.
        rows = list(private[spec["nodes"][-1]["in"]["in"]].values())
        if temporal:
            answer = baseline_caller(bundle, item)
            scored = score_item(conn, item, bundle, answer, False)
            scored["ms"] = round((monotonic() - start) * 1000, 1)
            scored["bundle_chars"] = len(json.dumps(bundle, ensure_ascii=False))
            out.append(scored)
        else:
            out.append({"id": item["id"], "lang": item.get("lang"), "kind": item.get("kind"),
                        "metrics": path_metrics(bundle["hits"], item["expected_paths"]),
                        "facets": facet_results(rows, item.get("controls", [])),
                        "ms": round((monotonic() - start) * 1000, 1)})
        if cancelled():
            raise TimeoutError("evaluation_budget_exceeded")
    return out


def evaluate(conn, spec, production, embedder, snapshot_id, digest, *, assets=ASSETS,
             cancelled=lambda: False):
    """Return an aggregate with fixed gates; no question, locator or note text.

    Snapshot binding and suite files are checked before any model invocation.
    Both lanes execute in one job. Unjudged items never count as a pass.
    """
    for candidate in (spec, production):
        if validate(candidate, snapshot_id, digest)["status"] != "ok":
            raise ValueError("invalid_spec")
    paths = {"ordinary": assets / "golden.jsonl", "protected_facets": assets / "q2-controls.json",
             "e1_temporal": assets / "e1-temporal.json"}
    digests = {name: _digest(path) for name, path in paths.items()}
    golden = [json.loads(line) for line in paths["ordinary"].read_text().splitlines() if line.strip()]
    q2 = json.loads(paths["protected_facets"].read_text())
    e1 = json.loads(paths["e1_temporal"].read_text())
    if q2["db_sha256"] != digest or not e1["gates"].get("declared_before_candidate_results"):
        raise ValueError("protected_snapshot_or_gates_mismatch")
    ordinary = [dict(item, controls=q2["queries"].get(item["id"], [])) for item in golden]
    if len({i["id"] for i in ordinary}) != len(ordinary) or not ordinary or not e1["items"]:
        raise ValueError("invalid_suite")
    if validate_gold(conn, e1["items"]):
        raise ValueError("e1_gold_not_anchored")
    lanes = {}
    for name, candidate in (("baseline", production), ("candidate", spec)):
        ordinary_rows = _score(conn, candidate, ordinary, embedder, snapshot_id, digest, cancelled=cancelled)
        temporal_rows = _score(conn, candidate, e1["items"], embedder, snapshot_id, digest,
                               temporal=True, cancelled=cancelled)
        judged = [r for r in ordinary_rows if r["metrics"] is not None]
        lanes[name] = {"ordinary": {key: _mean([r["metrics"] for r in judged], key)
                                     for key in ("recall@5", "recall@10", "mrr@10")},
                       "facets": {r["id"]: r["facets"] for r in ordinary_rows if r["facets"]},
                       "temporal": temporal_summary(temporal_rows),
                       "ordinary_ids": [r["id"] for r in ordinary_rows if r["metrics"] is not None],
                       "temporal_rows": temporal_rows}
    b, c = lanes["baseline"], lanes["candidate"]
    bs, cs = b["temporal"], c["temporal"]
    bt, ct = bs["by_category"], cs["by_category"]
    def rate(summary, cat, key):
        return summary.get(cat, {}).get(key)
    gates = {}
    answer = rate(ct, "temporal_order", "answer_correct")
    base_answer = rate(bt, "temporal_order", "answer_correct")
    gates["G1_temporal_answer"] = answer is not None and base_answer is not None and answer >= max(.70, base_answer + .25)
    seed = next((r for r in c["temporal_rows"] if r["id"] == "t01"), None)
    gates["G2_seed"] = bool(seed and seed.get("answer_correct") and seed.get("provenance_correct"))
    ab = cs["abstention"]
    gates["G3_abstention"] = ab["recall"] is not None and ab["precision"] is not None and ab["recall"] >= .8 and ab["precision"] >= .8
    gates["G4_ordinary_no_regression"] = all(rate(ct, "ordinary", key) is not None and
        rate(bt, "ordinary", key) is not None and rate(ct, "ordinary", key) >= rate(bt, "ordinary", key)
        for key in ("recall@5", "recall@15", "mrr")) and all(not r["applied"] for r in c["temporal_rows"]
        if r["category"] in {"ordinary", "authority"})
    bc = {r["id"]: r for r in b["temporal_rows"]}
    gates["G5_authority_provenance"] = all(r.get("controls") == bc[r["id"]].get("controls") and
        (not r.get("answer_correct") or r.get("provenance_correct", False)) for r in c["temporal_rows"]
        if r["category"] == "authority")
    gates["G6_latency"] = cs["latency_ms"]["p95"] <= bs["latency_ms"]["p95"] + 250 and cs["latency_ms"]["max"] <= 3000
    gates["G7_bounds"] = cs["assistant_hits"] == 0 and cs["max_bundle_chars"] <= 40000
    gates["Q2_protected_facets"] = bool(b["facets"]) and all(
        all(not baseline or candidate for baseline, candidate in zip(flags, c["facets"].get(ident, []), strict=True))
        for ident, flags in b["facets"].items()) and set(b["facets"]) == set(c["facets"])
    gates["ordinary_no_regression"] = bool(b["ordinary_ids"]) and all(c["ordinary"][key] is not None and
        b["ordinary"][key] is not None and c["ordinary"][key] >= b["ordinary"][key]
        for key in ("recall@5", "recall@10", "mrr@10"))
    # Explicitly provide only aggregate metrics and item ids/verdicts, never full scorer rows.
    def public(lane):
        return {"ordinary": lane["ordinary"], "temporal": lane["temporal"],
                "facets_pass": {ident: all(flags) for ident, flags in lane["facets"].items()},
                "temporal_items": _safe_rows([{"id": r["id"], "pass": bool(r.get("answer_correct", False))}
                                              for r in lane["temporal_rows"]])}
    return {"schema": "quoth-lab-eval/v1", "spec_hash": spec_hash(spec), "production_hash": spec_hash(production),
            "snapshot": {"id": snapshot_id, "db_sha256": digest},
            "suites": {**digests, "abstention": digests["e1_temporal"]},
            "gates_digest": hashlib.sha256(json.dumps(e1["gates"], sort_keys=True).encode()).hexdigest(),
            "baseline": public(b), "candidate": public(c), "gates": gates,
            "eligible": all(gates.values())}


def persist(state_dir: Path, kind: str, payload: dict) -> tuple[str, Path]:
    """Immutable bounded aggregate/candidate file; caller supplies isolated lab dir."""
    if kind not in {"evals", "candidates"}:
        raise ValueError("invalid state kind")
    content = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(content) > 256_000 or any(key in payload for key in ("question", "query", "text", "locator")):
        raise ValueError("unsafe aggregate")
    digest = hashlib.sha256(content).hexdigest()
    directory = Path(state_dir) / kind
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest = directory / (digest + ".json")
    tmp = directory / ("." + digest + "." + os.urandom(8).hex() + ".tmp")
    created = False
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        try:
            os.link(tmp, dest)
            created = True
        except FileExistsError:
            if dest.read_bytes() != content:
                raise ValueError("hash collision or corrupt state")
    finally:
        tmp.unlink(missing_ok=True)
    if kind == "evals":
        # Prune only aggregate eval files, never candidates. Keep 50 per spec,
        # 500 globally; stale/corrupt files are ignored, never trusted as gates.
        files = sorted(directory.glob("[0-9a-f]*.json"), key=lambda p: p.stat().st_mtime_ns, reverse=True)
        count = {}
        for position, path in enumerate(files):
            try:
                identity = json.loads(path.read_text())["spec_hash"]
            except (OSError, ValueError, KeyError):
                identity = "invalid"
            count[identity] = count.get(identity, 0) + 1
            if position >= 500 or count[identity] > 50:
                path.unlink(missing_ok=True)
    total = sum(path.stat().st_size for group in ("evals", "candidates")
                for path in (Path(state_dir) / group).glob("[0-9a-f]*.json"))
    if total > 50_000_000:
        if kind == "evals":
            # Never evict immutable exported candidates.
            for path in sorted(directory.glob("[0-9a-f]*.json"), key=lambda p: p.stat().st_mtime_ns):
                if total <= 50_000_000:
                    break
                total -= path.stat().st_size
                path.unlink(missing_ok=True)
        if total > 50_000_000 or not dest.exists():
            if kind == "candidates" and created:
                dest.unlink(missing_ok=True)
            raise ValueError("lab_state_capacity")
    return digest, dest
