"""Per-row observation and aggregate metrics over prefetch candidates (no text in the output).

Candidate lists come from the sidecar's dry-run ``detail.candidates``: one ranked list per
store (``vault``, ``claims``, ``owner``, ``assistant``), each item flagged ``injected`` when it
reached the recall block. Expected and stale sources are matched against those items. A
claims item with `history_of` is a past value served under its current claim; it matches
gold like any claim (a `follow: edits` anchor resolves to it).
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
    if key == "claim":
        # An anchor `eval run` did not resolve (it rewrites every anchor to a locator first):
        # a claim with the labelled text (no edit or supersede is followed without the ledger).
        from ..ledger.core import normalize
        return normalize(str(item.get("text") or "")) == normalize(value["text"])
    text = str(item.get("text") or "").casefold()
    return value.casefold() in text  # claim_text_contains / text_contains


def observe(row: dict, candidates: dict, *, sources: list | None = None, groups: list | None = None) -> dict:
    """Content-free per-row observation: ranks, injected matches, stale and negative leaks,
    and for all-of rows the first vault rank and injection of each group."""
    expect = row["expect"]
    sources = expect["sources"] if sources is None else sources
    groups = expect.get("groups") if groups is None else groups
    stale = expect.get("stale", [])
    negatives = expect.get("negatives", [])
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
        negative_injected = [it for it in injected if any(matches(it, store, s) for s in negatives)]
        stores[store] = {"expected": len(wanted), "ranks": first, "candidates": len(items),
                         "injected": len(injected), "injected_matched": len(hit_injected),
                         "stale_injected": len(stale_injected), "negative_injected": len(negative_injected),
                         # Past values served under their current claim (`history_of`): a past-intent turn.
                         "history_injected": sum(1 for it in injected if it.get("history_of") is not None)}
    block = [(store, it) for store in BLOCK_ORDER for it in (candidates.get(store) or []) if it.get("injected")]
    block_rank = next((i for i, (store, it) in enumerate(block, 1)
                       if any(matches(it, store, s) for s in sources)), None)
    vault_items = candidates.get("vault") or []
    group_obs = None
    if groups:
        group_obs = []
        for g in groups:
            ranks = [next((i for i, it in enumerate(vault_items, 1) if matches(it, "vault", s)), None)
                     for s in g["any_of"] if s["store"] == "vault"]
            group_obs.append({"rank": min((r for r in ranks if r is not None), default=None),
                              "injected": any(any(matches(it, st, s) for s in g["any_of"]) for st, it in block)})
    return {"id": row["id"], "kind": row["kind"], "origin": row["origin"], "lang": row["lang"],
            "split": row.get("split"), "abstain_reason": row.get("abstain_reason"), "hops": row.get("hops"),
            "abstain": bool(expect["abstain"]), "answerable": bool(sources),
            "claims_judged": bool(expect.get("claims_judged")) and not expect["abstain"],
            "has_stale": bool(stale), "has_negatives": bool(negatives), "groups": group_obs,
            "stores": stores, "block": len(block), "block_rank": block_rank}


def _mean(values) -> float | None:
    values = list(values)
    return round(sum(values) / len(values), 4) if values else None


def _rr(rank: int | None) -> float:
    return 1.0 / rank if rank is not None and rank <= MRR_CUTOFF else 0.0


# Stores whose gold a set gives for every answerable row: an injected item there that is not
# gold is known wrong. Other stores (claims, episode lanes) are judged on a row only when it
# says so (`claims_judged`); elsewhere an injected item that is not gold is unknown, not wrong.
JUDGED_STORES = ("vault",)


def _judged_precision(rows: list[dict]) -> float | None:
    """Share of injected items known right among injected items known right or wrong, per row,
    averaged. Unlike `injected_precision`, an unlabelled claim or episode does not count as
    wrong, so a gate is not rewarded for injecting no claims on a set without claims gold."""
    shares = []
    for o in rows:
        right = known = 0
        for name, st in o["stores"].items():
            right += st["injected_matched"]
            judged = name in JUDGED_STORES or (name == "claims" and o.get("claims_judged"))
            known += st["injected"] if judged else st["injected_matched"]
        if known:
            shares.append(right / known)
    return _mean(shares)


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
        "judged_precision": _judged_precision(with_block),
        "empty_rate": _mean(o["block"] == 0 for o in obs),
        "abstain_nonempty_rate": _mean(o["block"] > 0 for o in abstain),
        "stale_leak": sum(any(s["stale_injected"] for s in o["stores"].values()) for o in stale),
        "stale_leak_items": sum(s["stale_injected"] for o in stale for s in o["stores"].values()),
        "stores": {},
    }
    negative = [o for o in obs if o.get("has_negatives")]
    if negative:
        # Rows with judged look-alikes where one was injected (lower is better).
        out["negative_leak_rate"] = _mean(any(s["negative_injected"] for s in o["stores"].values())
                                          for o in negative)
    grouped = [o for o in obs if o.get("groups")]
    if grouped:
        # All-of rows: every group needed. `all@k` every group's vault rank within k;
        # `group_recall@10` the share of groups found; `injected_all_rate` every group injected.
        out["groups"] = {"n": len(grouped),
                         **{f"all@{k}": _mean(all(g["rank"] is not None and g["rank"] <= k for g in o["groups"])
                                              for o in grouped) for k in (5, 10)},
                         "group_recall@10": _mean(sum(g["rank"] is not None and g["rank"] <= 10 for g in o["groups"])
                                                  / len(o["groups"]) for o in grouped),
                         "injected_all_rate": _mean(all(g["injected"] for g in o["groups"]) for o in grouped)}
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
        if store == "claims":
            # Rows where a past value was injected (the past path ran and found one). A past value
            # that matches `follow: edits` gold counts as a hit like any injected claim.
            st["history_rate"] = _mean(o["stores"][store].get("history_injected", 0) > 0 for o in obs)
        judged = [o for o in obs if o.get("claims_judged")] if store == "claims" else []
        if judged:
            # Answerable rows whose claims gold the owner judged complete (`expect.claims_judged`):
            # every claim injected there is known right or wrong. `judged_precision` is the
            # mean share of right ones over judged rows with a claim injected (None: none was).
            hit = [o for o in judged if o["stores"][store]["injected"]]
            st["judged_n"] = len(judged)
            st["judged_items_mean"] = _mean(o["stores"][store]["injected"] for o in judged)
            st["judged_precision"] = _mean(o["stores"][store]["injected_matched"] / o["stores"][store]["injected"]
                                           for o in hit)
        if injected_rows:
            st["injected_precision"] = _mean(
                o["stores"][store]["injected_matched"] / o["stores"][store]["injected"]
                for o in injected_rows if o["answerable"]) if any(o["answerable"] for o in injected_rows) else None
        out["stores"][store] = st
    return out


# Dimensions every receipt breaks down by, and v2 ones it adds when any row sets them.
DIMS = ("kind", "origin", "lang")
OPTIONAL_DIMS = ("split", "abstain_reason", "hops")
Z95 = 1.959964


def wilson(successes: int, n: int, z: float = Z95) -> list | None:
    """95% Wilson score interval of a rate (sound at small n and near 0 or 1)."""
    if n == 0:
        return None
    p = successes / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def intervals(obs: list[dict]) -> dict:
    """Wilson intervals of the headline rates: what a set of this size can tell apart."""
    vault = [o for o in obs if o["stores"]["vault"]["expected"]]
    answerable = [o for o in obs if o["answerable"]]
    abstain = [o for o in obs if o["abstain"]]
    hit = lambda o, k: any(r is not None and r <= k for r in o["stores"]["vault"]["ranks"])
    return {"vault.hit@5": wilson(sum(hit(o, 5) for o in vault), len(vault)),
            "vault.hit@10": wilson(sum(hit(o, 10) for o in vault), len(vault)),
            "injected_hit_rate": wilson(sum(o["block_rank"] is not None for o in answerable), len(answerable)),
            "abstain_nonempty_rate": wilson(sum(o["block"] > 0 for o in abstain), len(abstain))}


def aggregate(obs: list[dict]) -> dict:
    out = {"overall": block(obs), "ci95": intervals(obs)}
    for dim in DIMS:
        out[f"by_{dim}"] = {value: block([o for o in obs if o[dim] == value])
                            for value in sorted({o[dim] for o in obs})}
    for dim in OPTIONAL_DIMS:
        values = sorted({o.get(dim) for o in obs if o.get(dim) is not None}, key=str)
        if values:
            out[f"by_{dim}"] = {str(v): block([o for o in obs if o.get(dim) == v]) for v in values}
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
        **({"all@10": {o["id"]: all(g["rank"] is not None and g["rank"] <= 10 for g in o["groups"])
                       for o in obs if o.get("groups")}} if any(o.get("groups") for o in obs) else {}),
    }
