"""Per-row observation and aggregate metrics over prefetch candidates (no text in the output).

Candidate lists come from the sidecar's dry-run ``detail.candidates``: one ranked list per
store (``vault``, ``claims``, ``owner``, ``assistant``), each item flagged ``injected`` when it
reached the recall block. Expected and stale sources are matched against those items.
"""
from __future__ import annotations

import math

from .format import STORES

K_VALUES = (1, 3, 5, 10)
MRR_CUTOFF = 10
# Order of sections in the injected recall block (format_recall, then the owner lane before
# the assistant lane).
BLOCK_ORDER = ("claims", "vault", "owner", "assistant")


def matches(item: dict, store: str, src: dict) -> bool:
    """Does a candidate of `store` satisfy one expected/stale source `{store, match}`?"""
    if src["store"] != store:
        return False
    (key, value), = src["match"].items()
    if key == "path":
        return item.get("path") == value
    if key == "locator":
        return item.get("locator") == value or (store == "claims" and f"claim:{item.get('id')}" == value)
    if key == "subject_key":
        return item.get("subject_key") == value
    text = str(item.get("text") or "").casefold()
    return value.casefold() in text  # claim_text_contains / text_contains


def observe(row: dict, candidates: dict, *, sources: list | None = None) -> dict:
    """Content-free per-row observation: ranks, injected matches, stale leaks."""
    expect = row["expect"]
    sources = expect["sources"] if sources is None else sources
    stale = expect.get("stale", [])
    stores = {}
    for store in STORES:
        items = candidates.get(store) or []
        wanted = [s for s in sources if s["store"] == store]
        first = []
        for src in wanted:
            rank = next((i for i, it in enumerate(items, 1) if matches(it, store, src)), None)
            first.append(rank)
        injected = [it for it in items if it.get("injected")]
        hit_injected = [it for it in injected if any(matches(it, store, s) for s in wanted)]
        stale_injected = [it for it in injected if any(matches(it, store, s) for s in stale)]
        stores[store] = {"expected": len(wanted), "ranks": first, "candidates": len(items),
                         "injected": len(injected), "injected_matched": len(hit_injected),
                         "stale_injected": len(stale_injected)}
    block = [(store, it) for store in BLOCK_ORDER for it in (candidates.get(store) or []) if it.get("injected")]
    block_rank = next((i for i, (store, it) in enumerate(block, 1)
                       if any(matches(it, store, s) for s in sources)), None)
    return {"id": row["id"], "kind": row["kind"], "origin": row["origin"], "lang": row["lang"],
            "abstain": bool(expect["abstain"]), "answerable": bool(sources),
            "has_stale": bool(stale), "stores": stores, "block": len(block), "block_rank": block_rank}


def _mean(values) -> float | None:
    values = list(values)
    return round(sum(values) / len(values), 4) if values else None


def _rr(rank: int | None) -> float:
    return 1.0 / rank if rank is not None and rank <= MRR_CUTOFF else 0.0


def block(obs: list[dict]) -> dict:
    """Aggregate metrics of a group of observations."""
    answerable = [o for o in obs if o["answerable"]]
    abstain = [o for o in obs if o["abstain"]]
    with_block = [o for o in answerable if o["block"]]
    stale = [o for o in obs if o["has_stale"]]
    out = {
        "n": len(obs), "answerable": len(answerable), "abstain": len(abstain),
        "injected_hit_rate": _mean(o["block_rank"] is not None for o in answerable),
        "injected_mrr": _mean(_rr(o["block_rank"]) for o in answerable),
        "injected_precision": _mean(
            sum(s["injected_matched"] for s in o["stores"].values()) / o["block"] for o in with_block),
        "empty_rate": _mean(o["block"] == 0 for o in obs),
        "abstain_nonempty_rate": _mean(o["block"] > 0 for o in abstain),
        "stale_leak": sum(any(s["stale_injected"] for s in o["stores"].values()) for o in stale),
        "stale_leak_items": sum(s["stale_injected"] for o in stale for s in o["stores"].values()),
        "stores": {},
    }
    for store in STORES:
        rows = [o for o in obs if o["stores"][store]["expected"]]
        injected_rows = [o for o in obs if o["stores"][store]["injected"]]
        st = {"n": len(rows),
              "injected_rate": _mean(o["stores"][store]["injected"] > 0 for o in obs),
              "candidates_mean": _mean(o["stores"][store]["candidates"] for o in obs)}
        if rows:
            for k in K_VALUES:
                st[f"hit@{k}"] = _mean(any(r is not None and r <= k for r in o["stores"][store]["ranks"])
                                       for o in rows)
                st[f"recall@{k}"] = _mean(
                    sum(r is not None and r <= k for r in o["stores"][store]["ranks"])
                    / len(o["stores"][store]["ranks"]) for o in rows)
            st["mrr"] = _mean(_rr(min((r for r in o["stores"][store]["ranks"] if r is not None), default=None))
                              for o in rows)
            st["injected_hit_rate"] = _mean(o["stores"][store]["injected_matched"] > 0 for o in rows)
        if injected_rows:
            st["injected_precision"] = _mean(
                o["stores"][store]["injected_matched"] / o["stores"][store]["injected"]
                for o in injected_rows if o["answerable"]) if any(o["answerable"] for o in injected_rows) else None
        out["stores"][store] = st
    return out


def aggregate(obs: list[dict]) -> dict:
    out = {"overall": block(obs)}
    for dim in ("kind", "origin", "lang"):
        out[f"by_{dim}"] = {value: block([o for o in obs if o[dim] == value])
                            for value in sorted({o[dim] for o in obs})}
    return out


def percentile(values: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile (the same rule as quality.percentile)."""
    if not values:
        return None
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def latency(samples: list[dict]) -> dict:
    stages = sorted({k for s in samples for k in s})
    out = {}
    for stage in stages:
        values = [s[stage] for s in samples if isinstance(s.get(stage), (int, float))]
        out[stage] = {"n": len(values), "p50": percentile(values, 0.5), "p95": percentile(values, 0.95),
                      "max": max(values) if values else None}
    return out


def _first(ranks) -> int | None:
    return min((r for r in ranks if r is not None and r <= MRR_CUTOFF), default=None)


def items(obs: list[dict]) -> dict:
    """Ids only, the pairs of `eval compare`'s paired tests: per-row vault hit@5 and hit@10
    (hit@10 is the gate's lost-gold basis), the first vault rank (None past rank 10), injected
    hit and the first expected source's position in the block, and whether an abstain row got
    anything injected."""
    vault = [o for o in obs if o["stores"]["vault"]["expected"]]
    return {
        "vault_hit@5": {o["id"]: any(r is not None and r <= 5 for r in o["stores"]["vault"]["ranks"])
                        for o in vault},
        "vault_hit@10": {o["id"]: any(r is not None and r <= 10 for r in o["stores"]["vault"]["ranks"])
                         for o in vault},
        "vault_rank": {o["id"]: _first(o["stores"]["vault"]["ranks"]) for o in vault},
        "injected_hit": {o["id"]: o["block_rank"] is not None for o in obs if o["answerable"]},
        "injected_rank": {o["id"]: o["block_rank"] for o in obs if o["answerable"]},
        "abstain_injected": {o["id"]: o["block"] > 0 for o in obs if o["abstain"]},
    }
