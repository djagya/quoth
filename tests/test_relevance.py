"""Relevance gate: signals, decisions and reasons, prefetch injection (off = unchanged,
gate before the cut, a status line when nothing clears), dry-run and log reporting, and the
sweep's replay math."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json

import pytest

from quoth import relevance, sidecar
from quoth.evals import sweep as SW
from quoth.relevance import Gate

from test_sidecar import snapshot_db, state_db  # noqa: F401  (fixtures)


# ------------------------------------------------------------------ signals

def test_terms_drop_function_words_and_fold_inflections():
    assert relevance.terms("What is the rent for the flat?") == {"rent", "flat"}
    # Stems: one ending stripped, then 6 letters; numbers stay whole.
    assert relevance.terms("проекта") == relevance.terms("проекту") == {"проект"}
    assert relevance.terms("in 2024 I adopted 3 cats") == {"2024", "adopte", "cat"}
    assert relevance.terms("is it?") == frozenset()
    # Conversational filler is not content.
    assert relevance.terms("thanks, hello!") == relevance.terms("спасибо, привет") == frozenset()


@pytest.mark.parametrize("a,b", [
    ("Москва", "Москве"), ("машина", "машину"), ("чай", "чая"), ("работа", "работе"), ("врач", "врачу"),
    ("rent", "rents"), ("city", "cities"), ("ёлка", "елка"), ("проекта", "проекту"),
])
def test_short_inflections_fold_together(a, b):
    assert relevance.terms(a) == relevance.terms(b) != frozenset()


def test_coverage_is_the_share_of_query_words_found():
    q = relevance.terms("sourdough starter feeding ratio")
    assert relevance.coverage(q, "Feed the starter at a 1:5:5 ratio.") == 0.5
    assert relevance.coverage(q, "nothing related") == 0.0
    assert relevance.coverage(frozenset(), "anything") is None


# ------------------------------------------------------------------ decisions

def test_gate_off_passes_everything():
    gate = Gate()
    assert not gate.vault_on and not gate.claims_on
    assert gate.vault(0.01, 0.9, 0.0) == (True, "off")
    assert gate.claim(None, 0.0) == (True, "off")


@pytest.mark.parametrize("cosine,top,lexical,matched,expected", [
    (0.70, 0.70, 0.0, 0, (True, "cosine")),
    (0.40, 0.70, 0.0, 0, (False, "below_floor")),
    (0.55, 0.70, 0.0, 0, (False, "gap")),          # over the floor, but 0.15 below the top hit
    (0.40, 0.70, 0.8, 4, (True, "lexical")),       # rescued by its words
    (0.40, 0.70, 1.0, 1, (False, "below_floor")),  # one shared word is not a rescue
    (None, 0.70, 0.0, 0, (True, "unscored")),      # never drop what cannot be judged
])
def test_vault_decisions_and_reasons(cosine, top, lexical, matched, expected):
    gate = Gate(vault_min_cosine=0.5, vault_max_gap=0.1, vault_lexical_rescue=0.75)
    assert gate.vault(cosine, top, lexical, matched) == expected


def test_short_turns_are_not_rescued_by_one_word():
    gate = Gate(vault_min_cosine=0.6, vault_lexical_rescue=0.5)
    hits = [{"text": "The kite notes from the beach.", "cosine": 0.41},
            {"text": "Kite lines and kite knots at the beach.", "cosine": 0.40}]
    one = relevance.judge_vault(gate, relevance.terms("kites?"), hits)
    assert [(j["lexical"], j["matched"], j["gate"]) for j in one] == [(1.0, 1, "below_floor")] * 2
    two = relevance.judge_vault(gate, relevance.terms("kites at the beach"), hits)
    assert [j["gate"] for j in two] == ["lexical", "lexical"]
    assert relevance.judge_vault(gate, relevance.terms("thanks, hello"), hits)[0]["lexical"] is None


def test_a_rescue_alone_is_refused():
    with pytest.raises(ValueError, match="vault_lexical_rescue"):
        Gate(vault_lexical_rescue=0.5)
    p = argparse.ArgumentParser()
    relevance.add_arguments(p)
    with pytest.raises(SystemExit):
        relevance.from_args(p.parse_args(["--vault-min-cosine", "off", "--vault-max-gap", "off",
                                          "--vault-lexical-rescue", "0.5"]), p)
    # With the serving defaults' floor and gap a rescue is coherent.
    assert relevance.from_args(p.parse_args(["--vault-lexical-rescue", "0.5"]), p).vault_lexical_rescue == 0.5
    status = Gate(vault_max_gap=0.1, vault_lexical_rescue=0.5).status()
    assert status["vault_on"] and not status["claims_on"]


def test_gap_is_compared_on_rounded_scores():
    assert Gate(vault_max_gap=0.1).vault(0.6, 0.7, None) == (True, "cosine")


@pytest.mark.parametrize("gate,cosine,lexical,expected", [
    (Gate(claims_min_cosine=0.6), 0.65, 0.0, (True, "cosine")),
    (Gate(claims_min_cosine=0.6), 0.55, 0.0, (False, "below_floor")),
    (Gate(claims_min_cosine=0.6), None, 0.0, (True, "unscored")),   # word-only path not gated
    (Gate(claims_min_lexical=0.5), None, 0.25, (False, "weak_words")),
    (Gate(claims_min_lexical=0.5), None, 0.5, (True, "lexical")),
    (Gate(claims_min_cosine=0.6, claims_min_lexical=0.5), 0.55, 0.5, (True, "lexical")),
])
def test_claim_decisions_and_reasons(gate, cosine, lexical, expected):
    assert gate.claim(cosine, lexical) == expected


def test_flags_round_trip_through_argparse():
    gate = Gate(vault_min_cosine=0.52, vault_max_gap=0.08, claims_min_lexical=0.34)
    p = argparse.ArgumentParser()
    relevance.add_arguments(p)
    assert relevance.from_args(p.parse_args(gate.flags())) == gate
    # No flags: the serving defaults; their flags are none, and `off` turns a default check off.
    assert relevance.from_args(p.parse_args([])) == relevance.default_gate()
    assert relevance.default_gate().flags() == []
    assert relevance.from_args(p.parse_args(Gate().flags())) == Gate()
    assert Gate().flags() == ["--vault-min-cosine", "off", "--vault-max-gap", "off"]
    assert Gate.from_dict(gate.to_dict()) == gate and Gate.from_dict(None) == Gate()
    assert gate.label() == "vault-min-cosine=0.52 vault-max-gap=0.08 claims-min-lexical=0.34"
    assert Gate().label() == "no gate"


# ------------------------------------------------------------------ prefetch

class _ScoredEvidence:
    """Evidence stand-in with dense cosines, as ReadOnlyEvidence returns with_scores."""

    queries = object()  # a real evidence service has one; claims stay lexical (empty index)

    def __init__(self, hits):
        self.hits = hits

    def bundle(self, query, k, model, **_):
        return {"hits": [dict(h) for h in self.hits[:k]]}


HITS = [{"locator": "a.md:1-2", "text": "Feed the sourdough starter at 1:5:5.", "note_date": None, "cosine": 0.71},
        {"locator": "b.md:1-2", "text": "The rent is due on the first.", "note_date": None, "cosine": 0.52},
        {"locator": "c.md:1-2", "text": "Basil grows in the south bed.", "note_date": None, "cosine": 0.44},
        {"locator": "d.md:1-2", "text": "Unrelated starter motor notes.", "note_date": None, "cosine": 0.40}]


def _app(tmp_path, state_db, gate=None, hits=HITS):
    return sidecar.Quoth(ledger_path=tmp_path / "l.db", state_db=state_db, evidence=_ScoredEvidence(hits),
                         evidence_budget=5.0, gate=gate)


def test_gate_off_injects_the_top_hits(tmp_path, state_db):
    plain = _app(tmp_path, state_db, Gate())
    try:
        out = plain.prefetch({"query": "how do I feed the sourdough starter", "dry_run": True,
                              "k_candidates": 4})
        assert out["evidence"] == 3 and all(h["locator"] in out["text"] for h in HITS[:3])
        vault = out["detail"]["candidates"]["vault"]
        assert [c["injected"] for c in vault] == [True, True, True, False]
        assert [c["pool"] for c in vault] == [True] * 4  # all judged; the top 3 that pass are injected
        assert {c["gate"] for c in vault} == {"off"} and all(c["passed"] for c in vault)
        assert plain.health()["relevance_gate"] == Gate().status()
    finally:
        plain.close()


def test_gate_drops_weak_hits_and_reports_why(tmp_path, state_db):
    app = _app(tmp_path, state_db, Gate(vault_min_cosine=0.5, vault_max_gap=0.15))
    try:
        out = app.prefetch({"query": "how do I feed the sourdough starter", "dry_run": True,
                            "k_candidates": 4})
        assert out["evidence"] == 1 and "a.md:1-2" in out["text"] and "b.md:1-2" not in out["text"]
        vault = out["detail"]["candidates"]["vault"]
        assert [(c["passed"], c["gate"]) for c in vault] == [
            (True, "cosine"), (False, "gap"), (False, "below_floor"), (False, "below_floor")]
        assert [c["injected"] for c in vault] == [True, False, False, False]
        assert [c["pool"] for c in vault] == [True] * 4
        assert vault[0]["lexical"] == 1.0 and vault[1]["lexical"] == 0.0
        assert [e["locator"] for e in out["detail"]["evidence"]] == ["a.md:1-2"]
    finally:
        app.close()


def test_gate_says_so_when_nothing_clears_the_bar(tmp_path, state_db):
    app = _app(tmp_path, state_db, Gate(vault_min_cosine=0.8))
    try:
        out = app.prefetch({"query": "what is the capital of Peru"})
        assert out["evidence"] == 0 and out["evidence_status"] == "ok"
        assert out["text"] == ("<quoth-recall>\n" + sidecar.VAULT_NOTES["none_relevant"] + "\n</quoth-recall>")
        assert not any(h["locator"] in out["text"] for h in HITS)
    finally:
        app.close()


def test_lexical_rescue_keeps_a_named_match(tmp_path, state_db):
    app = _app(tmp_path, state_db, Gate(vault_min_cosine=0.6, vault_lexical_rescue=0.5))
    try:
        out = app.prefetch({"query": "basil south bed"})
        assert "a.md:1-2" in out["text"] and "c.md:1-2" in out["text"] and "b.md:1-2" not in out["text"]
    finally:
        app.close()


def test_claims_gate_drops_incidental_word_matches(tmp_path, state_db):
    from quoth.ledger import core
    app = _app(tmp_path, state_db, Gate(claims_min_lexical=0.5), hits=[])
    try:
        with app.lock:
            for text in ("Alex prefers green tea in the morning.", "The garden has a tea rose."):
                cid = core.propose(app.conn, text=text, kind="preference", origin="assistant_proposal",
                                   proposer="t", reason="r")["claim"]["id"]
                core.accept(app.conn, cid, actor="t")
        out = app.prefetch({"query": "which green tea do I drink in the morning", "dry_run": True})
        assert out["claims"] == 1 and "green tea" in out["text"] and "tea rose" not in out["text"]
        claims = {c["text"]: c for c in out["detail"]["candidates"]["claims"]}
        rose = claims["The garden has a tea rose."]
        assert rose["pool"] and not rose["injected"] and rose["gate"] == "weak_words"
        # Only what reached the block counts as used (live turn).
        app.prefetch({"query": "which green tea do I drink in the morning"})
        with app.lock:
            used = dict(app.conn.execute("SELECT text, use_count FROM claims").fetchall())
        assert used["Alex prefers green tea in the morning."] == 1 and used["The garden has a tea rose."] == 0
    finally:
        app.close()


def test_claims_gate_judges_the_dense_pool_in_eval_and_live(tmp_path, state_db, monkeypatch):
    """Stale index ids above a claim at dense rank 14 (past the pool): live it is a word match
    (unscored); a deeper dry run must judge it the same, not by its rank-14 cosine, and so
    must the sweep."""
    from quoth.ledger import core
    app = _app(tmp_path, state_db, Gate(claims_min_cosine=0.8), hits=[])
    try:
        with app.lock:
            cid = core.propose(app.conn, text="Alex drinks oolong tea every morning.", kind="preference",
                               origin="assistant_proposal", proposer="t", reason="r")["claim"]["id"]
            core.accept(app.conn, cid, actor="t")
        dense = [(90001 + i, 0.95 - i / 100) for i in range(sidecar.CLAIM_POOL + 1)] + [(cid, 0.70)]
        monkeypatch.setattr(app, "_dense_work", lambda q, k, k_claims: {"hits": [], "claims": dense[:k_claims]})
        query = "which oolong tea do I drink in the morning"
        live = app.prefetch({"query": query, "dry_run": True})
        deep = app.prefetch({"query": query, "dry_run": True, "k_candidates": 20})
        assert live["claims"] == deep["claims"] == 1 and live["text"] == deep["text"]
        (cand,) = deep["detail"]["candidates"]["claims"]
        assert cand["score"] == 0.70 and cand["gate_score"] is None and cand["gate"] == "unscored"
        row = {"id": "r", "question": query,
               "expect": {"abstain": False, "sources": [{"store": "claims", "match": {"claim_text_contains": "oolong"}}]}}
        rec = SW.record(row, deep["detail"]["candidates"], row["expect"]["sources"])
        assert SW.observe("r", rec, Gate(claims_min_cosine=0.8))["stores"]["claims"]["injected"] == 1
    finally:
        app.close()


def test_prefetch_log_carries_gate_fields_without_text(tmp_path, state_db):
    from quoth.prefetch_log import PrefetchLog
    app = _app(tmp_path, state_db, Gate(vault_min_cosine=0.5))
    app.prefetch_log = PrefetchLog(tmp_path / "log", days=30)
    try:
        app.prefetch({"query": "how do I feed the sourdough starter", "session_id": "s1"})
        app.prefetch_log.flush()
        raw = "".join(p.read_text() for p in (tmp_path / "log").glob("*.jsonl"))
        turn = json.loads(raw.splitlines()[0])
        assert [(v["pool"], v["passed"], v["gate"], v["injected"]) for v in turn["stores"]["vault"]] == [
            (True, True, "cosine", True), (True, True, "cosine", True), (True, False, "below_floor", False),
            (True, False, "below_floor", False)]
        assert "served" not in json.dumps(turn)
        assert "sourdough" not in raw and "rent" not in raw
    finally:
        app.close()


def test_sidecar_cli_accepts_gate_flags(monkeypatch):
    seen = {}
    monkeypatch.setattr(sidecar, "Quoth", lambda **kw: seen.update(kw) or (_ for _ in ()).throw(SystemExit(0)))
    with pytest.raises(SystemExit):
        sidecar.main(["--ledger", ":memory:", "--state-db", "", "--vault-min-cosine", "0.55",
                      "--claims-min-lexical", "0.5"])
    assert seen["gate"] == replace(relevance.default_gate(), vault_min_cosine=0.55, claims_min_lexical=0.5)


# ------------------------------------------------------------------ sweep math

def _item(score, pool=True, match=(), lexical=None):
    out = {"score": score, "pool": pool, "injected": pool}
    if match:
        out["match"] = list(match)
    if lexical is not None:
        out["lexical"] = lexical
    return out


def _rec(vault, expected=1, abstain=False):
    stores = {s: {"expected": 0, "items": []} for s in ("claims", "owner", "assistant")}
    stores["vault"] = {"expected": expected, "items": vault}
    return {"abstain": abstain, "answerable": not abstain, "has_stale": False, "stores": stores}


ROWS = {
    # Gold at rank 1 (0.80) with two weak neighbours.
    "a": _rec([_item(0.80, match=[0]), _item(0.45), _item(0.42), _item(0.30, pool=False)]),
    # Gold at rank 2 (0.60), and a strong distractor.
    "b": _rec([_item(0.66), _item(0.60, match=[0]), _item(0.35)]),
    # Nothing relevant exists.
    "c": _rec([_item(0.48), _item(0.44), _item(0.40)], expected=0, abstain=True),
}


def test_sweep_replay_math():
    base, hits = SW.evaluate(ROWS, Gate())
    assert hits == {"a": True, "b": True}
    assert base["injected_precision"] == round((1 / 3 + 1 / 3) / 2, 4)
    assert base["abstain_nonempty_rate"] == 1.0 and base["items_mean"] == 3.0
    gated, hits = SW.evaluate(ROWS, Gate(vault_min_cosine=0.5))
    assert hits == {"a": True, "b": True}
    assert gated["injected_precision"] == round((1 / 1 + 1 / 2) / 2, 4)
    assert gated["abstain_nonempty_rate"] == 0.0 and gated["empty_rate"] == round(1 / 3, 4)
    assert gated["vault_hit@5"] == 1.0 and gated["abstain_injected_by_store"]["vault"] == 0.0
    # Too high a floor loses b's gold, from the ranked metrics too (original rank kept).
    lossy, hits = SW.evaluate(ROWS, Gate(vault_min_cosine=0.62))
    assert hits == {"a": True, "b": False} and lossy["vault_hit@5"] == 0.5


def test_sweep_recommends_the_best_config_within_the_loss_bound():
    receipt = {"schema": "quoth-eval-run/v1", "set": {"sha256": "x"}, "code": {"version": "t"},
               "config": {"k_candidates": 10}, "candidates": {"schema": SW.CANDIDATES_SCHEMA, "rows": ROWS}}
    result = SW.sweep(receipt, vault_gaps=(None,), vault_rescues=(None,), claims_lexicals=(None,))
    floors = {r["gate"]["vault_min_cosine"]: r for r in result["vault"]}
    assert floors[0.49]["eligible"] and not floors[0.61]["eligible"] and floors[0.61]["lost_hits"] == 1
    rec = result["recommended"]
    assert rec["abstain_nonempty_rate"] == 0.0 and rec["eligible"]
    # Equal outcomes: the gentlest floor wins (it keeps the most items).
    # Flags set exactly the replayed gate: the default gap is turned off.
    assert rec["gate"]["vault_min_cosine"] == 0.49
    assert result["flags"] == "--vault-min-cosine 0.49 --vault-max-gap off"
    text = SW.table(result)
    assert "Recommended: --vault-min-cosine 0.49 --vault-max-gap off" in text and "| no gate |" in text


def test_sweep_refuses_receipts_without_candidates():
    with pytest.raises(SW.SweepError, match="0.12.5"):
        SW.sweep({"schema": "quoth-eval-run/v1", "metrics": {}})


def test_record_keeps_signals_and_counts_words_on_the_injected_excerpt():
    """Words missing from a candidate are counted on what the block would inject (the excerpt)."""
    row = {"id": "r", "question": "how do I feed the sourdough starter",
           "expect": {"abstain": False, "sources": [{"store": "vault", "match": {"path": "a.md"}}]}}
    tail = "x " * 400 + "sourdough starter feed"  # past the injected excerpt: does not count
    cands = {"vault": [{"locator": "a.md:1-2", "path": "a.md", "score": 0.7, "pool": True, "injected": True,
                        "text": "Feed the sourdough starter."},
                       {"locator": "b.md:1-2", "path": "b.md", "score": 0.5, "pool": True, "injected": False,
                        "text": "Rent. " + tail}]}
    rec = SW.record(row, cands, row["expect"]["sources"])
    a, b = rec["stores"]["vault"]["items"]
    assert a == {"score": 0.7, "pool": True, "injected": True, "lexical": 1.0, "matched": 3, "match": [0],
                 "note": 0}
    assert b == {"score": 0.5, "pool": True, "injected": False, "lexical": 0.0, "note": 1}
    assert "text" not in json.dumps(rec) and "a.md" not in json.dumps(rec)


def test_record_judges_claims_by_their_gate_score():
    """A claim's recorded score is its cosine in the dense pool (`gate_score`), not a deeper rank."""
    row = {"id": "r", "question": "tea", "expect": {"abstain": False, "sources": []}}
    cands = {"claims": [{"id": 1, "score": 0.9, "gate_score": 0.9, "via": "dense", "pool": True, "text": "tea"},
                        {"id": 2, "score": 0.6, "gate_score": None, "via": "dense", "pool": False, "text": "x"}]}
    items = SW.record(row, cands, [])["stores"]["claims"]["items"]
    assert [(it["score"], it["pool"]) for it in items] == [(0.9, True), (None, False)]


def test_vault_pick_refills_a_dropped_slot_from_the_pool():
    gate = Gate(vault_min_cosine=0.5, vault_lexical_rescue=0.5)
    assert relevance.vault_pick(gate, [True, False, True, True, True], pool=5, k=3) == [0, 2, 3]
    assert relevance.vault_pick(gate, [True, False, True, True, True], pool=3, k=3) == [0, 2]
    assert relevance.vault_pick(gate, [False] * 4, pool=10, k=3) == []


def test_a_lexical_rescue_refills_a_slot_the_floor_dropped(tmp_path, state_db):
    """Gate before the cut: rank 4 (rescued by its words) takes the slot rank 2 lost."""
    hits = [dict(h) for h in HITS]
    hits[3] = {**hits[3], "text": "Basil in the south bed, by the starter shed.", "cosine": 0.40}
    app = _app(tmp_path, state_db, Gate(vault_min_cosine=0.6, vault_lexical_rescue=0.5), hits=hits)
    try:
        out = app.prefetch({"query": "basil south bed", "dry_run": True})
        assert [e["locator"] for e in out["detail"]["evidence"]] == ["a.md:1-2", "c.md:1-2", "d.md:1-2"]
    finally:
        app.close()


def test_the_gate_judges_the_injected_excerpt():
    """Words past the injected excerpt do not rescue a hit: the judged text is the injected text."""
    gate = Gate(vault_min_cosine=0.6, vault_lexical_rescue=0.5)
    far = {"text": "notes " * 100 + "basil south bed", "cosine": 0.4}
    near = {"text": "basil south bed " + "notes " * 100, "cosine": 0.4}
    far_j, near_j = relevance.judge_vault(gate, relevance.terms("basil south bed"), [far, near])
    assert far_j["gate"] == "below_floor" and near_j["gate"] == "lexical"
    assert len(relevance.vault_text(near)) == sidecar.PREFETCH_SNIPPET


def test_evidence_scores_survive_a_shortened_bundle(snapshot_db, monkeypatch):
    """The bundle's text ceiling can drop hits; the rest keep their own cosine (by locator)."""
    from quoth import evidence_service
    from quoth.evidence_service import ReadOnlyEvidence
    from quoth.serving import ServingSnapshot
    evidence = ReadOnlyEvidence(ServingSnapshot(str(snapshot_db)))
    try:
        full = evidence.bundle("tomatoes basil garden", 4, "hash-ngram", with_scores=True)["hits"]
        assert len(full) == 4 and all("cosine" in h for h in full)
        real = evidence_service.evidence_bundle
        monkeypatch.setattr(evidence_service, "evidence_bundle", lambda hits, k: real(hits[1:], k=k))
        short = evidence.bundle("tomatoes basil garden", 4, "hash-ngram", with_scores=True)["hits"]
        assert [(h["locator"], h["cosine"]) for h in short] == [(h["locator"], h["cosine"]) for h in full[1:]]
    finally:
        evidence.snapshot.close()
