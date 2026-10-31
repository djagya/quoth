"""Paired significance tests for two receipts of the same set (row ids are the pairs).

Golden sets are small (50–100 rows), so one row moves a rate by 1–2 points and an unpaired
margin is wide. Both receipts score the same rows, so the tests compare row by row:

- `mcnemar`: binary outcomes (hit@k, injected hit, an abstain row injected). Only rows that
  differ count; the exact two-sided binomial test on them.
- `permutation`: per-row scores (reciprocal rank). Sign-flip test on the paired
  differences of the mean: exact up to `EXACT_MAX` differing rows, otherwise a seeded
  Monte Carlo sample, so a result is reproducible.

Both return counts and a p-value only; nothing here sees question text.
"""
from __future__ import annotations

from itertools import product
from math import comb
import random

EXACT_MAX = 16
SAMPLES = 20000
SEED = 1729


def _pairs(a: dict, b: dict) -> list[tuple]:
    return [(a[i], b[i]) for i in sorted(a.keys() & b.keys())
            if a[i] is not None and b[i] is not None]


def binom_two_sided(k: int, n: int) -> float:
    """Exact two-sided p of k or fewer successes in n fair trials (doubled, capped at 1)."""
    if n == 0:
        return 1.0
    tail = sum(comb(n, i) for i in range(0, min(k, n - k) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def mcnemar(a: dict, b: dict) -> dict:
    """{id: bool} on each side. `gained`: false in A, true in B; `lost`: the reverse."""
    pairs = _pairs(a, b)
    gained = sum(1 for x, y in pairs if not x and y)
    lost = sum(1 for x, y in pairs if x and not y)
    return {"test": "mcnemar_exact", "n": len(pairs), "a": sum(bool(x) for x, _ in pairs),
            "b": sum(bool(y) for _, y in pairs), "gained": gained, "lost": lost,
            "p": round(binom_two_sided(min(gained, lost), gained + lost), 4)}


def permutation(a: dict, b: dict, *, samples: int = SAMPLES, seed: int = SEED) -> dict:
    """{id: float} on each side: two-sided sign-flip test on the mean paired difference."""
    pairs = _pairs(a, b)
    diffs = [float(y) - float(x) for x, y in pairs]
    n = len(diffs)
    mean = sum(diffs) / n if n else 0.0
    nonzero = [d for d in diffs if d != 0]
    out = {"test": "paired_permutation", "n": n, "mean_a": _avg(x for x, _ in pairs),
           "mean_b": _avg(y for _, y in pairs), "delta": round(mean, 4), "differing": len(nonzero)}
    if not nonzero:
        return {**out, "p": 1.0, "method": "exact"}
    observed = abs(sum(nonzero)) - 1e-12
    if len(nonzero) <= EXACT_MAX:
        hits = sum(1 for signs in product((1, -1), repeat=len(nonzero))
                   if abs(sum(s * d for s, d in zip(signs, nonzero))) >= observed)
        return {**out, "p": round(hits / 2 ** len(nonzero), 4), "method": "exact"}
    rng = random.Random(seed)
    hits = sum(1 for _ in range(samples)
               if abs(sum(d if rng.random() < 0.5 else -d for d in nonzero)) >= observed)
    return {**out, "p": round((hits + 1) / (samples + 1), 4), "method": f"monte_carlo:{samples}"}


def _avg(values) -> float | None:
    values = list(values)
    return round(sum(values) / len(values), 4) if values else None


def rr(rank) -> float:
    return 1.0 / rank if rank else 0.0


def paired(a: dict, b: dict) -> dict:
    """Paired tests over the per-row `items` of two receipts, for every item map both have."""
    ia, ib = a.get("items") or {}, b.get("items") or {}
    out = {}
    for name in ("vault_hit@5", "vault_hit@10", "injected_hit", "abstain_injected"):
        if name in ia and name in ib:
            out[name] = mcnemar(ia[name], ib[name])
    for name in ("vault_rank", "injected_rank"):
        if name in ia and name in ib:
            out[name.replace("_rank", "_rr")] = permutation(
                {i: rr(r) for i, r in ia[name].items()}, {i: rr(r) for i, r in ib[name].items()})
    return out
