"""`eval compare A B`: gate-style diff of two run receipts, tolerances as data.

Exit-code contract (shared by the CLI): 0 pass, 1 regression past a tolerance (or lost
gold), 2 unusable input (unreadable receipt or tolerance file), 3 the runs are not
comparable or B is invalid (different set, B gold drift excessive, B not scored).

Tolerances are a JSON document; every metric is addressed by a dotted path into the
receipt (``metrics.overall.stores.vault.hit@10``). Rules per metric: ``max_drop``
(higher is better), ``max_rise`` (lower is better), ``max_rise_ratio`` (relative rise,
for latency). A metric missing on either side is reported, never failed.
"""
from __future__ import annotations

import json
from pathlib import Path

from .run import Receipt

DEFAULT_TOLERANCES = {
    "schema": "quoth-eval-tolerances/v1",
    "require_same_set": True,
    "max_lost_gold": 0,
    "metrics": {
        # The nightly gate's source-retrieval metrics, on the vault store's candidates.
        "metrics.overall.stores.vault.hit@5": {"max_drop": 0.0},
        "metrics.overall.stores.vault.hit@10": {"max_drop": 0.0},
        "metrics.overall.stores.vault.mrr": {"max_drop": 0.02},
        "metrics.overall.stores.vault.recall@10": {"max_drop": 0.0},
        # What actually reaches the model.
        "metrics.overall.injected_hit_rate": {"max_drop": 0.02},
        "metrics.overall.injected_mrr": {"max_drop": 0.02},
        "metrics.overall.injected_precision": {"max_drop": 0.05},
        "metrics.overall.abstain_nonempty_rate": {"max_rise": 0.05},
        "metrics.overall.stale_leak": {"max_rise": 0},
        "metrics.overall.stores.claims.hit@5": {"max_drop": 0.02},
        "metrics.overall.stores.owner.hit@5": {"max_drop": 0.05},
        "metrics.overall.stores.assistant.hit@5": {"max_drop": 0.05},
        "latency_ms.wall.p95": {"max_rise_ratio": 0.5},
    },
}


def load_tolerances(path: str | Path | None) -> dict:
    if path is None:
        return DEFAULT_TOLERANCES
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("metrics", {}), dict):
        raise ValueError("tolerances must be an object with a metrics map")
    return {**DEFAULT_TOLERANCES, **data}


def _get(doc: dict, dotted: str):
    cur = doc
    for part in _split(dotted):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur if isinstance(cur, (int, float)) and not isinstance(cur, bool) else None


def _split(dotted: str) -> list[str]:
    # Metric names contain "@" but never ".", except the leading path parts.
    return dotted.split(".")


def compare(a: Receipt | dict, b: Receipt | dict, tolerances: dict | None = None) -> dict:
    """Return {"result": pass|fail|incomparable, "exit": 0|1|3, "checks": [...], ...} (no text)."""
    a = a.data if isinstance(a, Receipt) else a
    b = b.data if isinstance(b, Receipt) else b
    tol = tolerances or DEFAULT_TOLERANCES
    problems = []
    if tol.get("require_same_set", True) and a.get("set", {}).get("sha256") != b.get("set", {}).get("sha256"):
        problems.append("different_set")
    if b.get("status", "ok") != "ok":
        problems.append(f"b_status:{b.get('status')}")
    if not b.get("metrics"):
        problems.append("b_not_scored")
    checks = []
    for path, rule in tol.get("metrics", {}).items():
        va, vb = _get(a, path), _get(b, path)
        check = {"metric": path, "a": va, "b": vb, "rule": rule}
        if va is None or vb is None:
            check["result"] = "missing"
        else:
            delta = vb - va
            check["delta"] = round(delta, 4)
            ok = True
            if "max_drop" in rule and -delta > rule["max_drop"] + 1e-9:
                ok = False
            if "max_rise" in rule and delta > rule["max_rise"] + 1e-9:
                ok = False
            if "max_rise_ratio" in rule and va > 0 and delta / va > rule["max_rise_ratio"] + 1e-9:
                ok = False
            check["result"] = "pass" if ok else "fail"
        checks.append(check)
    items_a = (a.get("items") or {}).get("vault_hit@10", {})
    items_b = (b.get("items") or {}).get("vault_hit@10", {})
    lost = sorted(i for i, hit in items_a.items() if hit and items_b.get(i) is False)
    gained = sorted(i for i, hit in items_a.items() if not hit and items_b.get(i) is True)
    lost_fail = len(lost) > int(tol.get("max_lost_gold", 0))
    if problems:
        result, code = "incomparable", 3
    elif lost_fail or any(c["result"] == "fail" for c in checks):
        result, code = "fail", 1
    else:
        result, code = "pass", 0
    return {"schema": "quoth-eval-compare/v1", "result": result, "exit": code, "problems": problems,
            "lost_gold": lost, "gained_gold": gained, "gold_drift": {"a": a.get("gold_drift"),
                                                                    "b": b.get("gold_drift")},
            "checks": checks}
