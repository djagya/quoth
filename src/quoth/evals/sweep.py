"""`eval sweep`: replay the relevance gate over a run's recorded candidates, per threshold.

`eval run` records, per row, every candidate's signals (cosine, lexical coverage), which
expected sources it matches (indices, no text), whether it is in the turn's live pool and
whether it was injected. The sweep applies `relevance.Gate` configs to those records
exactly as the sidecar would (judge every pool candidate, then inject the best that pass:
`relevance.vault_pick`, the first PREFETCH_CLAIMS passing claims) and scores each with the
harness metrics (`metrics.block`). Ranked metrics count an expected source only where it
would pass the gate (at its original rank).

Configs are judged against a reference: by default the live gate (the gate the run
recorded, else `recall_defaults.GATE`), or no gate (``reference="none"``, the sweep up to
0.18.0). A config is eligible when, on every receipt swept, no injected metric (the block's
hit rate, the vault's and the claims' injected hit rates) and abstain correctness lose more
than ``max_loss`` against the reference, and it loses at most ``max_lost_hits`` answerable
rows the reference injects (0 against the live gate). It recommends the eligible config
that maximises injected precision plus abstain correctness (1 - abstain_nonempty_rate) on
the pooled rows, only when that beats the reference. Ranked candidate metrics (hit@5,
recall@10) are reported, not constrained: live injects only from the pool. Every config
keeps the reference's short-query rule, and claims configs its vault rules. Output holds
configs and aggregates only.

A run with a reranker (`eval run --rerank-model`, 0.13.2) also records each candidate's
rerank score; the sweep then adds rerank configs (floor x gap x widen on the vault, a floor
on claims), replayed through the same `relevance` functions as the sidecar. Each scored
candidate's recorded scoring time (`rerank_ms`) replays the live `--rerank-budget`: a pair
that would end past it loses its score on that row (the cosine gate then decides, as
live), and a config whose p95 scoring time exceeds the budget is not eligible.
"""
from __future__ import annotations

from itertools import product
import math

from .. import recall_defaults as D
from .. import relevance
from . import metrics as M
from .format import STORES

CANDIDATES_SCHEMA = "quoth-eval-candidates/v1"
SCHEMA = "quoth-eval-sweep/v2"
DEFAULT_MAX_LOSS = 0.01
REFERENCES = ("live", "none")
DEFAULT_REFERENCE = "live"
VAULT_GAPS = (None, 0.02, 0.04, 0.06, 0.08, 0.1, 0.15, 0.2)
VAULT_RESCUES = (None, 0.5, 0.75, 1.0)
CLAIMS_LEXICALS = (None, 0.25, 0.34, 0.5, 0.67, 1.0)
# Only what reaches the block is constrained: gold at ranks live never injects (vault 4-10)
# may fail the gate without costing the assistant anything.
CONSTRAINTS = ("injected_hit_rate", "vault_injected_hit_rate", "claims_injected_hit_rate")
PREFETCH_CLAIMS = D.PREFETCH_CLAIMS
PREFETCH_EVIDENCE = D.PREFETCH_EVIDENCE
COMBINE_TOP = 5
# Rerank scores are sigmoid probabilities, bunched near 0 and 1: a log-like floor grid.
RERANK_FLOORS = (0.001, 0.002, 0.005, 0.01, 0.02, 0.03, 0.05, 0.07, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4,
                 0.5, 0.6, 0.7, 0.8, 0.9)
RERANK_GAPS = (None, 0.2, 0.4, 0.6, 0.8, 0.9, 0.95)
RERANK_WIDENS = (None, 5, 10)
DEFAULT_RERANK_BUDGET = D.RERANK_BUDGET  # the sidecar's --rerank-budget
DEFAULT_EVIDENCE_BUDGET = D.EVIDENCE_BUDGET  # the sidecar's --evidence-budget: scoring also ends here


class SweepError(ValueError):
    pass


# ---------------------------------------------------------------- recording (eval run)

def record(row: dict, candidates: dict, sources: list, timings: dict | None = None) -> dict:
    """One row's candidates as signals and match indices (no text, no locators)."""
    from ..sidecar import prefetch_query
    query_terms = relevance.terms(prefetch_query(row["question"]))
    stale = row["expect"].get("stale", [])
    stores = {}
    for store in STORES:
        wanted = [s for s in sources if s["store"] == store]
        items = []
        notes: dict = {}  # a vault candidate's note as a per-row index (no paths in a receipt)
        for it in candidates.get(store) or []:
            lexical, matched = it.get("lexical"), it.get("matched")
            if store == "vault" and (lexical is None or matched is None):
                lexical, matched = relevance.overlap(query_terms, relevance.vault_text(it))
            elif store == "claims" and lexical is None:
                lexical = relevance.coverage(query_terms, it.get("text") or "")
            # The gate judges a claim by its cosine in the turn's dense pool (`gate_score`).
            score = it.get("gate_score") if store == "claims" else it.get("score")
            item = {"score": score, "pool": bool(it.get("pool")), "injected": bool(it.get("injected"))}
            match = [i for i, s in enumerate(wanted) if M.matches(it, store, s)]
            # Optional keys are left out when empty (receipt size): lexical, matched, match, stale.
            if lexical is not None:
                item["lexical"] = lexical
            if matched:
                item["matched"] = matched
            if match:
                item["match"] = match
            if store == "vault" and it.get("path") is not None:
                item["note"] = notes.setdefault(it["path"], len(notes))
            if it.get("named"):
                item["named"] = True
            if any(M.matches(it, store, s) for s in stale):
                item["stale"] = True
            if it.get("rerank") is not None:  # 0.13.2 with a reranker loaded
                item["rerank"] = it["rerank"]
                if it.get("rerank_ms") is not None:
                    item["rerank_ms"] = it["rerank_ms"]
            items.append(item)
        stores[store] = {"expected": len(wanted), "items": items}
    # The query's content-word count, for the short-query rule (receipts before 0.18 lack
    # it: a replay then skips that rule).
    out = {"abstain": bool(row["expect"]["abstain"]), "answerable": bool(sources),
           "has_stale": bool(stale), "terms": len(query_terms), "stores": stores}
    dense = [(timings or {}).get(k) for k in ("vault", "claims_dense")]
    if dense[0] is not None and any("rerank" in it for it in stores["vault"]["items"]):
        # The row's evidence search time: live scoring also ends at the evidence deadline.
        out["dense_ms"] = round(sum(v for v in dense if v is not None), 1)
    return out


# ---------------------------------------------------------------- replay

def _rerank(items: list[dict]) -> list | None:
    """The recorded rerank scores in candidate order (None where unscored), or None."""
    return [it.get("rerank") for it in items] if any("rerank" in it for it in items) else None


def _passes(gate: relevance.Gate, store: str, items: list[dict], terms: int | None = None) -> list[bool]:
    if store == "vault":
        signals = [{"cosine": it["score"], "lexical": it.get("lexical"), "matched": it.get("matched", 0),
                    "named": bool(it.get("named"))} for it in items]
        return [ok for ok, _ in relevance.decide_vault(gate, signals, PREFETCH_EVIDENCE, _rerank(items), terms)]
    if store == "claims":
        return [gate.claim(it["score"], it.get("lexical"), it.get("rerank"))[0] for it in items]
    return [True] * len(items)


def _injected(gate: relevance.Gate, store: str, items: list[dict], passed: list[bool]) -> list[dict]:
    """What reaches the block, in block order, as the sidecar picks it: the best pool
    candidates that pass (vault: `relevance.vault_pick`; claims: the first PREFETCH_CLAIMS)."""
    if store == "vault":
        pool = sum(it["pool"] for it in items)
        return [items[i] for i in relevance.vault_pick(gate, passed, pool, PREFETCH_EVIDENCE, _rerank(items),
                                                       notes=[it.get("note") for it in items])]
    if store == "claims":
        return [it for it, ok in zip(items, passed) if it["pool"] and ok][:PREFETCH_CLAIMS]
    return [it for it in items if it["injected"]]


def observe(row_id: str, rec: dict, gate: relevance.Gate) -> dict:
    """`metrics.observe`'s observation of a recorded row under `gate`."""
    stores, injected_items = {}, {}
    for store in STORES:
        st = rec["stores"].get(store) or {"expected": 0, "items": []}
        items = st["items"]
        passed = _passes(gate, store, items, rec.get("terms"))
        chosen = _injected(gate, store, items, passed)
        ranks = [next((p for p, (it, ok) in enumerate(zip(items, passed), 1) if ok and i in it.get("match", ())),
                      None)
                 for i in range(st["expected"])]
        injected_items[store] = chosen
        stores[store] = {"expected": st["expected"], "ranks": ranks, "candidates": len(items),
                         "injected": len(chosen), "injected_matched": sum(bool(it.get("match")) for it in chosen),
                         "stale_injected": sum(bool(it.get("stale")) for it in chosen)}
    block = [it for store in M.BLOCK_ORDER for it in injected_items[store]]
    block_rank = next((i for i, it in enumerate(block, 1) if it.get("match")), None)
    return {"id": row_id, "abstain": rec["abstain"], "answerable": rec["answerable"],
            "has_stale": rec["has_stale"], "stores": stores, "block": len(block), "block_rank": block_rank}


def _unscored(items: list[dict], keep) -> list[dict]:
    return [it if keep(i) else {k: v for k, v in it.items() if k not in ("rerank", "rerank_ms")}
            for i, it in enumerate(items)]


def fit(gate: relevance.Gate, rec: dict, budget_ms: float | None,
        evidence_ms: float | None = DEFAULT_EVIDENCE_BUDGET * 1000) -> tuple[dict, float | None, float | None]:
    """(the row as a live turn would score it, the scoring ms this config waits for, the ms
    it has; None when unknown). It has `budget_ms`, or less when the row's recorded
    evidence search (`dense_ms`) leaves less of `evidence_ms`. Live scores the vault hits
    it reranks first, then the pool's claims; a pair ending past that is lost, and a vault
    list not fully scored is not used at all (cosine gate)."""
    if budget_ms is None or not gate.rerank_any:
        return rec, None, None
    if rec.get("dense_ms") is not None and evidence_ms is not None:
        budget_ms = max(0.0, min(budget_ms, evidence_ms - rec["dense_ms"]))
    vault = (rec["stores"].get("vault") or {"expected": 0, "items": []})
    items = vault["items"]
    depth = min(gate.rerank_depth(PREFETCH_EVIDENCE), len(items))
    if not any("rerank" in it for it in items[:depth]) or any("rerank_ms" not in it for it in items[:depth]):
        return rec, None, None  # nothing scored, or no recorded timing (receipt before 0.13.2)
    spent = sum(it["rerank_ms"] for it in items[:depth])
    claims = (rec["stores"].get("claims") or {"expected": 0, "items": []})
    stores = dict(rec["stores"])
    if spent > budget_ms:
        stores["vault"] = {**vault, "items": _unscored(items, lambda i: False)}
        stores["claims"] = {**claims, "items": _unscored(claims["items"], lambda i: False)}
        return {**rec, "stores": stores}, spent, budget_ms
    clock, keep = spent, set()
    for i, it in enumerate(claims["items"]):
        if "rerank_ms" in it and it["pool"]:
            clock += it["rerank_ms"]
            if clock <= budget_ms:
                keep.add(i)
    stores["claims"] = {**claims, "items": _unscored(claims["items"], lambda i: i in keep)}
    if gate.claims_min_rerank is not None:
        spent = clock
    return {**rec, "stores": stores}, spent, budget_ms


def _observations(rows: dict, gate: relevance.Gate, budget_ms: float | None = None,
                  evidence_ms: float | None = DEFAULT_EVIDENCE_BUDGET * 1000) -> tuple[list, list]:
    """(observations, (scoring ms, the ms it has) per row with a known scoring time) of
    `rows` under `gate`."""
    fitted, times = {}, []
    for rid, rec in rows.items():
        fitted[rid], spent, limit = fit(gate, rec, budget_ms, evidence_ms)
        if spent is not None:
            times.append((spent, limit))
    return [observe(rid, rec, gate) for rid, rec in fitted.items()], times


def _hits(obs: list[dict]) -> dict:
    """{answerable row id: the block holds an expected source}."""
    return {o["id"]: o["block_rank"] is not None for o in obs if o["answerable"]}


def evaluate(rows: dict, gate: relevance.Gate, budget_ms: float | None = None,
             evidence_ms: float | None = DEFAULT_EVIDENCE_BUDGET * 1000) -> tuple[dict, dict]:
    """(summary row, {row id: injected hit}) of one gate config; with `budget_ms`, rerank
    scores replay the live budget (`fit`)."""
    obs, times = _observations(rows, gate, budget_ms, evidence_ms)
    return _summary(obs, times, gate, budget_ms), _hits(obs)


def _summary(obs: list[dict], times: list, gate: relevance.Gate, budget_ms: float | None) -> dict:
    """The sweep's metrics of a group of observations (one receipt's, or several pooled)."""
    b = M.block(obs)
    vault, claims = b["stores"]["vault"], b["stores"]["claims"]
    abstain = [o for o in obs if o["abstain"]]
    out = {"gate": gate.to_dict(),
           "vault_hit@5": vault.get("hit@5"), "vault_recall@10": vault.get("recall@10"),
           "vault_mrr": vault.get("mrr"),
           "claims_hit@5": claims.get("hit@5"), "claims_recall@10": claims.get("recall@10"),
           "vault_injected_hit_rate": vault.get("injected_hit_rate"),
           "claims_injected_hit_rate": claims.get("injected_hit_rate"),
           "injected_hit_rate": b["injected_hit_rate"], "injected_mrr": b["injected_mrr"],
           "injected_precision": b["injected_precision"], "abstain_nonempty_rate": b["abstain_nonempty_rate"],
           "empty_rate": b["empty_rate"], "stale_leak": b["stale_leak"],
           "items_mean": M._mean(o["block"] for o in obs),
           "vault_items_mean": M._mean(o["stores"]["vault"]["injected"] for o in obs),
           "claims_items_mean": M._mean(o["stores"]["claims"]["injected"] for o in obs),
           "abstain_injected_by_store": {s: M._mean(o["stores"][s]["injected"] > 0 for o in abstain)
                                         for s in STORES}}
    if budget_ms is not None and gate.rerank_any:
        out["rerank_p95_ms"] = M.percentile([t for t, _ in times], 0.95)
        out["rerank_in_time"] = M._mean(t <= limit for t, limit in times)
        # p95 of how far scoring overruns each row's time (budget, or what the search left).
        out["rerank_p95_over_ms"] = M.percentile([round(t - limit, 1) for t, limit in times], 0.95)
    return out


def objective(row: dict) -> tuple:
    """Higher is better: precision + abstain correctness, then hits, then the gentler and
    simpler config (more items kept, fewer thresholds set)."""
    abstain = row["abstain_nonempty_rate"]
    return (round((row["injected_precision"] or 0.0) + (0.0 if abstain is None else 1.0 - abstain), 4),
            row["injected_hit_rate"] or 0.0, row["items_mean"] or 0.0,
            -sum(v is not None for v in (row["gate"] or {}).values()))


def beats(row: dict, ref: dict) -> bool:
    """`row` scores better than `ref` on the objective. The simplicity tie-break does not
    count: the reference's outcome with fewer thresholds is no reason to change the gate."""
    return objective(row)[:3] > objective(ref)[:3]


def ranked(rows: list[dict]) -> list[dict]:
    """Eligible rows best first, one per distinct outcome (the simplest config of each)."""
    out, seen = [], set()
    for r in sorted((r for r in rows if r["eligible"]), key=objective, reverse=True):
        key = tuple(r.get(c) for c, _ in COLUMNS)
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _losses(row: dict, base: dict) -> dict:
    return {m: round(base[m] - row[m], 4) for m in CONSTRAINTS
            if base.get(m) is not None and row.get(m) is not None}


# ---------------------------------------------------------------- grids

def _steps(values: list[float]) -> list[float]:
    """0.01 thresholds spanning the recorded scores (a floor outside them changes nothing)."""
    if not values:
        return []
    lo, hi = math.floor(min(values) * 100), math.ceil(max(values) * 100)
    return [round(i / 100, 2) for i in range(lo, hi + 1)]


def _pool_scores(rows: dict, store: str) -> list[float]:
    return [it["score"] for rec in rows.values() for it in (rec["stores"].get(store) or {}).get("items", [])
            if it["pool"] and it["score"] is not None]


def _short(gate: relevance.Gate | None = None) -> dict:
    """The short-query rule a config keeps: the gate's, or the serving default's when it
    sets none (no grid sweeps that rule)."""
    if gate is not None and gate.vault_short_terms is not None:
        return {k: getattr(gate, k) for k in relevance.SHORT_FIELDS}
    return {k: D.GATE[k] for k in relevance.SHORT_FIELDS}


def keep_for_vault(gate: relevance.Gate | None = None) -> dict:
    """What every vault config keeps of `gate` (the reference): its claims rules and its
    short-query rule."""
    return {**_claims_part(gate.to_dict() if gate else {}), **_short(gate)}


def keep_for_claims(gate: relevance.Gate | None = None) -> dict:
    """What every claims config keeps of `gate` (the reference): its whole vault part."""
    return {**_vault_part(gate.to_dict() if gate else {}), **_short(gate)}


def _gate(keep: dict | None, **swept) -> relevance.Gate:
    return relevance.Gate(**{**(keep or {}), **swept})


def vault_grid(rows: dict, cosines=None, gaps=VAULT_GAPS, rescues=VAULT_RESCUES,
               keep: dict | None = None) -> list[relevance.Gate]:
    """Vault cosine configs; each keeps `keep` (default: the serving short-query rule)."""
    keep = keep_for_vault() if keep is None else keep
    cosines = [None, *(_steps(_pool_scores(rows, "vault")) if cosines is None else cosines)]
    out = []
    for c, g, r in product(cosines, gaps, rescues):
        if c is not None or g is not None:  # a rescue alone gates nothing (and is refused)
            out.append(_gate(keep, vault_min_cosine=c, vault_max_gap=g, vault_lexical_rescue=r))
    return out


def _scored(rows: dict, store: str) -> bool:
    return any("rerank" in it for rec in rows.values() for it in (rec["stores"].get(store) or {}).get("items", []))


def rerank_grid(rows: dict, floors=RERANK_FLOORS, gaps=RERANK_GAPS, widens=RERANK_WIDENS,
                depth: int | None = None, keep: dict | None = None) -> list[relevance.Gate]:
    """Vault rerank configs, when the run recorded rerank scores. A widen deeper than the
    recorded candidates (`depth`) cannot be replayed and is left out. Each keeps `keep`
    (default: the serving short-query rule)."""
    if not _scored(rows, "vault"):
        return []
    keep = keep_for_vault() if keep is None else keep
    widens = [w for w in widens if w is None or depth is None or w <= depth]
    return [g for g in (_gate(keep, vault_min_rerank=f, vault_max_rerank_gap=g, vault_rerank_widen=w)
                        for f, g, w in product([None, *floors], gaps, widens)) if g.rerank_on]


def claims_rerank_grid(rows: dict, floors=RERANK_FLOORS, keep: dict | None = None) -> list[relevance.Gate]:
    keep = keep_for_claims() if keep is None else keep
    return [_gate(keep, claims_min_rerank=f) for f in floors] if _scored(rows, "claims") else []


def claims_grid(rows: dict, cosines=None, lexicals=CLAIMS_LEXICALS, keep: dict | None = None) -> list[relevance.Gate]:
    """Claims configs; each keeps `keep` (default: the serving short-query rule, no vault floor)."""
    keep = keep_for_claims() if keep is None else keep
    cosines = [None, *(_steps(_pool_scores(rows, "claims")) if cosines is None else cosines)]
    return [g for g in (_gate(keep, claims_min_cosine=c, claims_min_lexical=x)
                        for c, x in product(cosines, lexicals)) if g.claims_on]


# ---------------------------------------------------------------- sweep

def rows_of(receipt: dict) -> dict:
    cands = receipt.get("candidates")
    if not isinstance(cands, dict) or cands.get("schema") != CANDIDATES_SCHEMA:
        raise SweepError("receipt has no per-candidate signals: rerun `quoth eval run` with quoth >= 0.12.5")
    return cands["rows"]


def reference_gate(receipt: dict, reference: str = DEFAULT_REFERENCE) -> relevance.Gate:
    """The gate a receipt's configs are judged against: `live`, the gate the run served with
    (its recorded `config.gate`), else the serving default (`recall_defaults.GATE`);
    `none`, no gate (the sweep up to 0.18.0)."""
    if reference not in REFERENCES:
        raise SweepError(f"unknown reference {reference!r}: one of {', '.join(REFERENCES)}")
    if reference == "none":
        return relevance.Gate()
    recorded = (receipt.get("config") or {}).get("gate")
    return relevance.Gate.from_dict(recorded) if isinstance(recorded, dict) else relevance.default_gate()


def _receipt_info(receipt: dict, rows: dict) -> dict:
    config = receipt.get("config") or {}
    return {"set_sha256": (receipt.get("set") or {}).get("sha256"),
            "version": (receipt.get("code") or {}).get("version"),
            "k_candidates": config.get("k_candidates"), "gate": config.get("gate"), "rows": len(rows)}


def _named(receipts) -> list[tuple[str, dict]]:
    """[(name, receipt)] from one receipt or (name, receipt) pairs; names are unique."""
    pairs = [("receipt", receipts)] if isinstance(receipts, dict) else list(receipts)
    if not pairs:
        raise SweepError("no receipt to sweep")
    names = [n for n, _ in pairs]
    if len(set(names)) != len(names):
        raise SweepError(f"receipt names repeat: {', '.join(sorted({n for n in names if names.count(n) > 1}))}")
    return pairs


def _against(row: dict, hits: dict, ref: dict, ref_hits: dict, max_loss: float,
             max_lost_hits: int | None) -> dict:
    """One receipt's verdict on a config against its reference: answerable rows the
    reference injects and the config does not (`lost_hits`) or the reverse (`gained_hits`),
    the constrained metrics' losses, the rise of abstain_nonempty_rate, and `eligible`."""
    lost = sum(1 for rid, hit in ref_hits.items() if hit and not hits.get(rid))
    gained = sum(1 for rid, hit in hits.items() if hit and not ref_hits.get(rid))
    loss = _losses(row, ref)
    a, b = row.get("abstain_nonempty_rate"), ref.get("abstain_nonempty_rate")
    rise = None if a is None or b is None else round(a - b, 4)
    over = (row.get("rerank_p95_over_ms") or 0) > 0
    eligible = (all(v <= max_loss + 1e-9 for v in loss.values()) and not over
                and (max_lost_hits is None or lost <= max_lost_hits)
                and (rise is None or rise <= max_loss + 1e-9))
    return {"lost_hits": lost, "gained_hits": gained, "loss": loss, "abstain_rise": rise,
            "over_budget": over, "eligible": eligible}


PER_SET = ("injected_hit_rate", "injected_precision", "abstain_nonempty_rate", "items_mean")


def sweep(receipts, *, reference: str = DEFAULT_REFERENCE, max_loss: float = DEFAULT_MAX_LOSS,
          max_lost_hits: int | None = None, vault_cosines=None, vault_gaps=VAULT_GAPS,
          vault_rescues=VAULT_RESCUES, claims_cosines=None, claims_lexicals=CLAIMS_LEXICALS,
          vault_reranks=RERANK_FLOORS, vault_rerank_gaps=RERANK_GAPS, vault_rerank_widens=RERANK_WIDENS,
          claims_reranks=RERANK_FLOORS, rerank_budget: float = DEFAULT_RERANK_BUDGET,
          evidence_budget: float = DEFAULT_EVIDENCE_BUDGET) -> dict:
    """Every config's metrics, the eligible ones, and the recommended gate (or None).

    `receipts`: one receipt, or [(name, receipt)] pairs. Each config is scored per receipt
    against that receipt's reference gate (`reference_gate`) and is eligible only where it
    is eligible on every receipt (`_against`); `max_lost_hits` defaults to 0 against the
    live gate and to no limit with ``reference="none"``. Configs rank by the objective on
    the pooled rows, and the pick must beat the pooled reference (`beats`), else it is None.
    `rerank_budget` is the live sidecar's --rerank-budget (seconds) rerank configs replay."""
    pairs = _named(receipts)
    if max_lost_hits is None and reference == "live":
        max_lost_hits = 0
    budget_ms, evidence_ms = rerank_budget * 1000, evidence_budget * 1000
    sets = []
    for name, receipt in pairs:
        prefix = f"{name}: " if len(pairs) > 1 else ""
        try:
            rows = rows_of(receipt)
        except SweepError as exc:
            raise SweepError(f"{prefix}{exc}") from None
        if not rows:
            raise SweepError(f"{prefix}receipt has no scored rows")
        gate = reference_gate(receipt, reference)
        obs, times = _observations(rows, gate, budget_ms, evidence_ms)
        sets.append({"name": name, "receipt": receipt, "rows": rows, "gate": gate, "obs": obs, "times": times,
                     "ref": _summary(obs, times, gate, budget_ms), "ref_hits": _hits(obs)})
    multi = len(sets) > 1
    pooled_rows = {f"{s['name']}/{rid}": rec for s in sets for rid, rec in s["rows"].items()} if multi \
        else sets[0]["rows"]
    gates = [s["gate"] for s in sets]
    same = all(g == gates[0] for g in gates)
    # What the grids keep of the reference (the serving default when receipts disagree).
    template = gates[0] if same else relevance.default_gate()
    ref = _summary([o for s in sets for o in s["obs"]], [t for s in sets for t in s["times"]], template, budget_ms)
    if not same:
        ref["gate"] = None  # a different reference per receipt (`sets`)
    ref.update(lost_hits=0, gained_hits=0, loss=_losses(ref, ref), abstain_rise=0.0, over_budget=False,
               eligible=True)

    def per_set(gate):
        for s in sets:
            obs, times = _observations(s["rows"], gate, budget_ms, evidence_ms)
            yield s, obs, times, _summary(obs, times, gate, budget_ms), _hits(obs)

    def scored(gate):
        verdicts, all_obs, all_times = {}, [], []
        for s, obs, times, row, hits in per_set(gate):
            verdicts[s["name"]] = {**{k: row.get(k) for k in PER_SET},
                                   **_against(row, hits, s["ref"], s["ref_hits"], max_loss, max_lost_hits)}
            all_obs += obs
            all_times += times
        row = _summary(all_obs, all_times, gate, budget_ms)
        a, b = row.get("abstain_nonempty_rate"), ref.get("abstain_nonempty_rate")
        row.update(lost_hits=sum(v["lost_hits"] for v in verdicts.values()),
                   gained_hits=sum(v["gained_hits"] for v in verdicts.values()),
                   loss=_losses(row, ref), abstain_rise=None if a is None or b is None else round(a - b, 4),
                   over_budget=any(v["over_budget"] for v in verdicts.values()),
                   eligible=all(v["eligible"] for v in verdicts.values()))
        if multi:
            row["sets"] = verdicts
        return row

    # The no-gate row, reported for scale (it is the reference itself with --reference none).
    base = ref if all(g == relevance.Gate() for g in gates) else scored(relevance.Gate())
    keep_v, keep_c = keep_for_vault(template), keep_for_claims(template)
    vault = [scored(g) for g in vault_grid(pooled_rows, vault_cosines, vault_gaps, vault_rescues, keep_v)]
    depths = [(s["receipt"].get("config") or {}).get("k_candidates") for s in sets]
    depth = min((d for d in depths if d is not None), default=None)
    vault_rerank = [scored(g) for g in rerank_grid(pooled_rows, vault_reranks, vault_rerank_gaps,
                                                   vault_rerank_widens, depth, keep_v)]
    claims = [scored(g) for g in claims_grid(pooled_rows, claims_cosines, claims_lexicals, keep_c)]
    claims += [scored(g) for g in claims_rerank_grid(pooled_rows, claims_reranks, keep_c)]
    combined = [scored(relevance.Gate(**{**_vault_part(v["gate"]), **_claims_part(c["gate"])}))
                for v, c in product(ranked(vault + vault_rerank)[:COMBINE_TOP], ranked(claims)[:COMBINE_TOP])]
    candidates = ranked(vault + vault_rerank + claims + combined)
    recommended = candidates[0] if candidates and beats(candidates[0], ref) else None
    vault_scored = [it["score"] is not None for rec in pooled_rows.values()
                    for it in (rec["stores"].get("vault") or {}).get("items", [])]
    served = [it for rec in pooled_rows.values()
              for it in (rec["stores"].get("vault") or {}).get("items", [])[:PREFETCH_EVIDENCE]]
    # Turns without scores keep the cosine gate: the best cosine-only vault config is the
    # fallback, unless it is no better than the reference, whose vault rules then stay.
    fallback = None
    if recommended is not None and relevance.Gate.from_dict(recommended["gate"]).rerank_on:
        cosine_best = next(iter(ranked(vault)), None)
        if template.vault_on and (cosine_best is None or not beats(cosine_best, ref)):
            cosine_best = {"gate": {**template.to_dict(), **_short(template)}}
        fallback = " ".join(relevance.Gate(**_vault_part(cosine_best["gate"])).flags()) if cosine_best else ""
    out = {"schema": SCHEMA, "reference": reference, "reference_gate": ref, "rows": len(pooled_rows),
           "max_loss": max_loss, "max_lost_hits": max_lost_hits, "constraints": list(CONSTRAINTS),
           "objective": "injected_precision + (1 - abstain_nonempty_rate)",
           "vault_scored_share": M._mean(vault_scored),
           "vault_reranked_share": M._mean("rerank" in it for it in served) if _scored(pooled_rows, "vault") else None,
           "baseline": base, "vault": vault, "vault_rerank": vault_rerank, "claims": claims, "combined": combined,
           "recommended": recommended,
           "flags": " ".join(relevance.Gate.from_dict(recommended["gate"]).flags()) if recommended else "",
           "fallback_flags": fallback, "rerank_budget": rerank_budget,
           # Rows whose live rerank of the 3 served hits would finish within the budget.
           "rerank_in_time": evaluate(pooled_rows, relevance.Gate(vault_rerank_widen=PREFETCH_EVIDENCE),
                                      budget_ms, evidence_ms)[0].get("rerank_in_time")
           if _scored(pooled_rows, "vault") else None,
           "sets": {}}
    if not multi:
        out["receipt"] = _receipt_info(sets[0]["receipt"], sets[0]["rows"])
    pick = relevance.Gate.from_dict(recommended["gate"]) if recommended else None
    for s in sets:  # per receipt: the reference and the pick, in full
        entry = {"receipt": _receipt_info(s["receipt"], s["rows"]),
                 "answerable": sum(bool(r.get("answerable")) for r in s["rows"].values()),
                 "abstain": sum(bool(r.get("abstain")) for r in s["rows"].values()),
                 "reference": s["ref"], "recommended": None}
        if pick is not None:
            row, hits = evaluate(s["rows"], pick, budget_ms, evidence_ms)
            entry["recommended"] = {**row, **_against(row, hits, s["ref"], s["ref_hits"], max_loss, max_lost_hits)}
        out["sets"][s["name"]] = entry
    return out


def _vault_part(gate: dict) -> dict:
    return {k: v for k, v in gate.items() if k.startswith("vault_")}


def _claims_part(gate: dict) -> dict:
    return {k: v for k, v in gate.items() if k.startswith("claims_")}


# ---------------------------------------------------------------- table

COLUMNS = (("vault_hit@5", "v.hit@5"), ("vault_injected_hit_rate", "v.inj.hit"),
           ("claims_injected_hit_rate", "c.inj.hit"), ("injected_hit_rate", "inj.hit"),
           ("injected_precision", "inj.prec"),
           ("abstain_nonempty_rate", "abst.nonempty"), ("empty_rate", "empty"), ("items_mean", "items"),
           ("vault_items_mean", "vault"), ("claims_items_mean", "claims"), ("lost_hits", "lost"),
           ("gained_hits", "gained"))
RERANK_COLUMNS = (("rerank_p95_ms", "p95 ms"), ("rerank_in_time", "in time"))
SET_COLUMNS = (("injected_hit_rate", "inj.hit"), ("injected_precision", "inj.prec"),
               ("abstain_nonempty_rate", "abst.nonempty"), ("items_mean", "items"), ("lost_hits", "lost"),
               ("gained_hits", "gained"))


def _label(gate: dict | None) -> str:
    return "a different gate per receipt" if gate is None else relevance.Gate.from_dict(gate).label()


def _fmt(value) -> str:
    if value is None:
        return "–"
    return f"{value:.3f}" if isinstance(value, float) else str(value)


def _changes(rows: list[dict]) -> list[dict]:
    """Rows whose metrics differ from the previous one (a floor curve without repeats)."""
    out, last = [], None
    for r in rows:
        key = tuple(r.get(c) for c, _ in COLUMNS)
        if key != last:
            out.append(r)
            last = key
    return out


def table(result: dict, top: int = 10) -> str:
    """Markdown tables: the vault floor curve, the best eligible configs per store and
    combined (each under no gate and the reference), then the pick, per receipt when
    several were swept."""
    def key(r):
        return tuple(r.get(c) for c, _ in COLUMNS)

    base, ref = result["baseline"], result.get("reference_gate") or result["baseline"]
    live = result.get("reference", "none") != "none"
    heads = [base] if ref is base else [base, ref]

    def name(r):
        if r is ref and live:
            return f"live: {_label(r['gate'])}"
        return _label(r["gate"])

    def section(title, rows, cols=COLUMNS):
        head = "| config | " + " | ".join(h for _, h in cols) + " | ok |"
        rule = "|---" * (len(cols) + 2) + "|"
        rows = [r for r in rows if all(key(r) != key(h) for h in heads)]  # no-op configs add nothing
        return [title, "", head, rule, *[f"| {name(r)} | " + " | ".join(_fmt(r.get(c)) for c, _ in cols)
                                         + f" | {'yes' if r['eligible'] else 'no'} |"
                                         for r in heads + rows], ""]

    curve = _changes([r for r in result["vault"]
                      if r["gate"]["vault_max_gap"] is None and r["gate"]["vault_lexical_rescue"] is None])
    sets = result.get("sets") or {}
    rows = result.get("rows", (result.get("receipt") or {}).get("rows"))
    against = (f"against the live gate, at most {result.get('max_lost_hits')} lost answerable rows" if live
               else "against no gate")
    out = [f"rows {rows}" + (f" in {len(sets)} receipts" if len(sets) > 1 else "")
           + f", {against}, max loss {result['max_loss']}, "
           f"vault candidates with a cosine {_fmt(result['vault_scored_share'])}, "
           f"ranked by {result['objective']}", "",
           *section("Vault floor only (rows where metrics change):", curve)]
    reranked = result.get("vault_rerank") or []
    if reranked:
        rerank_curve = _changes(sorted((r for r in reranked if r["gate"].get("vault_max_rerank_gap") is None
                                        and r["gate"].get("vault_rerank_widen") is None),
                                       key=lambda r: r["gate"].get("vault_min_rerank") or 0.0))
        out[0] += f", served vault hits with a rerank score {_fmt(result.get('vault_reranked_share'))}"
        if result.get("rerank_budget") is not None:
            out[0] += (f", rows whose 3 served hits are scored within the {result['rerank_budget']:g} s"
                       f" rerank budget {_fmt(result.get('rerank_in_time'))}")
        cols = COLUMNS + RERANK_COLUMNS
        out += [*section("Vault rerank floor only (rows where metrics change):", rerank_curve, cols),
                *section("Best eligible vault rerank configs (p95 scoring time within the budget):",
                         ranked(reranked)[:top], cols)]
    out += [*section("Best eligible vault configs:", ranked(result["vault"])[:top]),
            *section("Best eligible claims configs:", ranked(result["claims"])[:top]),
            *section("Best eligible combined configs:", ranked(result["combined"])[:top])]
    rec = result["recommended"]
    if rec:
        out.append(f"Recommended: {result['flags']}")
    elif live:
        out.append("Recommended: keep the live gate (no config beats it within the loss bounds)")
    else:
        out.append("Recommended: no gate (nothing beats it within the loss bound)")
    if result.get("fallback_flags") is not None:
        out.append("Turns without rerank scores keep the cosine gate: "
                   + (result["fallback_flags"] or "no cosine config is eligible (leave it off)"))
    labels = (("no gate", base),) + ((("live gate", ref),) if ref is not base else ()) + (("recommended", rec),)
    for label, row in labels:
        if row is not None:  # which stores still inject on rows that should get nothing
            out.append(f"Abstain rows injected by store, {label}: " + ", ".join(
                f"{s} {_fmt(v)}" for s, v in row["abstain_injected_by_store"].items()))
    if len(sets) > 1:
        cols = SET_COLUMNS
        out += ["", "Per receipt (reference, then the recommended config):", "",
                "| receipt | config | " + " | ".join(h for _, h in cols) + " | ok |",
                "|---" * (len(cols) + 3) + "|"]
        for set_name, entry in sets.items():
            for label, row in (("reference", entry["reference"]), ("recommended", entry["recommended"])):
                if row is None:
                    continue
                full = {"lost_hits": 0, "gained_hits": 0, "eligible": True, **row}
                out.append(f"| {set_name} | {label} | " + " | ".join(_fmt(full.get(c)) for c, _ in cols)
                           + f" | {'yes' if full['eligible'] else 'no'} |")
    return "\n".join(out) + "\n"
