"""Cross-encoder reranker (0.13.2): the deterministic fake, the service's load states, the
rerank gate decisions, prefetch with scores (gate, widen, claims), the cosine fallback when
scores are late, loading, unavailable or failing, default-off identity, and the sweep's
rerank replay (equal to an in-process run with the same flags)."""
from __future__ import annotations

import argparse
import json
import threading
import time

import pytest

from quoth import relevance, rerank, sidecar
from quoth.evals import InProcessTarget, load_set, run_set
from quoth.evals import sweep as SW
from quoth.relevance import Gate
from quoth.rerank import RerankService

from test_evals import SET, built  # noqa: F401  (fixture)
from test_relevance import HITS, _ScoredEvidence
from test_sidecar import snapshot_db, state_db  # noqa: F401  (fixtures)

QUERY = "how do I feed the sourdough starter"


# ------------------------------------------------------------------ the fake and the backend

def test_hash_reranker_is_deterministic_and_bounded():
    fake = rerank.get_reranker("hash-rerank")
    same, other = fake.score(QUERY, [QUERY, "Basil grows in the south bed."])
    assert same > 0.95 and other < 0.2
    assert fake.score(QUERY, [QUERY, "x"]) == fake.score(QUERY, [QUERY, "x"])
    assert all(isinstance(s, float) and 0.0 <= s <= 1.0 for s in fake.score("a", ["b", "", "a a a"]))
    # A deadline already passed scores nothing (pairs are scored one at a time).
    assert fake.score(QUERY, ["a", "b"], deadline=time.monotonic() - 1) == [None, None]
    assert rerank.sigmoid(-1000.0) == 0.0 and rerank.sigmoid(1000.0) == 1.0 and rerank.sigmoid(0.0) == 0.5


def test_passages_are_folded_and_bounded():
    assert rerank.passage("a\n\n  b\tc ") == "a b c"
    assert len(rerank.passage("x" * 5000)) == rerank.PASSAGE_CHARS


def test_missing_model_files_are_unavailable_not_downloaded(tmp_path):
    pytest.importorskip("onnxruntime")
    pytest.importorskip("tokenizers")
    with pytest.raises(rerank.RerankerUnavailable, match="--download"):
        rerank.get_reranker(rerank.DEFAULT_MODEL, cache_dir=str(tmp_path))
    assert not any(tmp_path.iterdir())
    with pytest.raises(ValueError, match="unknown reranker"):
        rerank.get_reranker("nope")


def test_model_specs_pin_one_revision_and_list_their_files():
    for name in ("bge-reranker-v2-m3-int8", "bge-reranker-v2-m3"):
        spec = rerank.MODELS[name]
        assert spec.repo == "onnx-community/bge-reranker-v2-m3-ONNX" and len(spec.revision) == 40
        assert spec.onnx_file in spec.files and "tokenizer.json" in spec.files
    assert rerank.MODELS["bge-reranker-v2-m3"].weights == ("onnx/model.onnx_data",)
    assert rerank.model_dir(rerank.MODELS[rerank.DEFAULT_MODEL], "/m") == "/m/onnx-community/bge-reranker-v2-m3-ONNX"


def test_check_command_reports_latency(capsys):
    assert rerank.main(["check", "--model", "hash-rerank"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["model"] == "hash-rerank" and len(out["scores"]) == 3
    assert out["scores"][0] > out["scores"][1] and set(out["ms_3_pairs"]) == {"median", "min", "max"}


# ------------------------------------------------------------------ service

class _Fixed:
    """A reranker with set scores per passage text (unknown text: 0.5); optional delay per pair."""

    def __init__(self, scores: dict, delay: float = 0.0, fail: bool = False, delays: dict | None = None):
        self.scores, self.delay, self.fail, self.calls = scores, delay, fail, []
        self.delays = delays or {}

    def score(self, query, texts, deadline=None):
        out = []
        for text in texts:
            if deadline is not None and time.monotonic() >= deadline:
                out.append(None)
                continue
            if self.fail and text != "warmup":
                raise RuntimeError("boom")
            time.sleep(self.delays.get(text, self.delay) if text != "warmup" else 0)
            self.calls.append(text)
            out.append(self.scores.get(text, 0.5))
        return out


def _service(model, **kw) -> RerankService:
    service = RerankService("hash-rerank", loader=lambda: model, **kw)
    assert service.wait() == "ready"
    return service


def test_service_loads_in_the_background_and_reports_failures():
    svc = _service(_Fixed({}))
    assert svc.status()["status"] == "ready" and svc.status()["load_ms"] is not None
    broken = RerankService("hash-rerank", loader=lambda: (_ for _ in ()).throw(rerank.RerankerUnavailable("x")))
    assert broken.wait() == "unavailable" and broken.model() is None
    assert RerankService("hash-rerank").wait() == "ready"  # the default loader builds the fake
    with pytest.raises(ValueError):
        RerankService("nope")


def test_rerank_flags_need_a_model():
    p = argparse.ArgumentParser()
    relevance.add_arguments(p)
    rerank.add_arguments(p)
    args = p.parse_args(["--vault-min-rerank", "0.3"])
    with pytest.raises(SystemExit):
        rerank.from_args(args, relevance.from_args(args), p)
    assert rerank.from_args(p.parse_args([]), Gate()) is None
    args = p.parse_args(["--rerank-model", "hash-rerank", "--claims-min-rerank", "0.2", "--rerank-budget", "0.4"])
    svc = rerank.from_args(args, relevance.from_args(args))
    assert svc.name == "hash-rerank" and svc.claims and svc.budget == 0.4


# ------------------------------------------------------------------ gate decisions

@pytest.mark.parametrize("score,top,expected", [
    (0.90, 0.90, (True, "rerank")),
    (0.20, 0.90, (False, "rerank_floor")),
    (0.50, 0.90, (False, "rerank_gap")),
    (None, 0.90, (False, "below_floor")),   # not scored: the cosine rules apply (0.40 < 0.5)
])
def test_rerank_decisions_and_reasons(score, top, expected):
    gate = Gate(vault_min_cosine=0.5, vault_min_rerank=0.3, vault_max_rerank_gap=0.3)
    assert gate.vault(0.40, 0.70, 0.0, 0, score, top) == expected


def test_rerank_fields_stay_out_of_the_gate_dict_until_set():
    assert Gate().to_dict() == {"vault_min_cosine": None, "vault_max_gap": None, "vault_lexical_rescue": None,
                                "claims_min_cosine": None, "claims_min_lexical": None}
    gate = Gate(vault_min_rerank=0.25, vault_max_rerank_gap=0.6, vault_rerank_widen=10, claims_min_rerank=0.1)
    assert gate.to_dict()["vault_rerank_widen"] == 10 and gate.rerank_on and gate.rerank_any
    p = argparse.ArgumentParser()
    relevance.add_arguments(p)
    assert relevance.from_args(p.parse_args(gate.flags())) == gate == Gate.from_dict(gate.to_dict())
    assert not Gate(claims_min_rerank=0.1).rerank_on and Gate(claims_min_rerank=0.1).rerank_any
    for bad in (0, 21, 2.5):
        with pytest.raises(ValueError, match="widen"):
            Gate(vault_rerank_widen=bad)


def test_claims_rerank_decides_scored_claims_only():
    gate = Gate(claims_min_lexical=0.5, claims_min_rerank=0.3)
    assert gate.claim(None, 0.0, 0.8) == (True, "rerank")
    assert gate.claim(0.9, 1.0, 0.1) == (False, "rerank_floor")
    assert gate.claim(None, 0.25, None) == (False, "weak_words")  # unscored: the word bar applies


def test_partial_scores_keep_the_cosine_gate_and_a_widen_reorders():
    gate = Gate(vault_min_cosine=0.5, vault_min_rerank=0.3, vault_rerank_widen=4)
    items = [{"cosine": c, "lexical": None, "matched": 0} for c in (0.71, 0.52, 0.44, 0.40)]
    full = [0.1, 0.2, 0.9, 0.8]
    assert relevance.rerank_usable(gate, full, 4, 3)
    assert [r for _, r in relevance.decide_vault(gate, items, 3, full)] == [
        "rerank_floor", "rerank_floor", "rerank", "rerank"]
    assert relevance.vault_order(gate, 4, 3, full) == [2, 3, 1]
    partial = [0.1, 0.2, 0.9, None]  # the widen's 4th hit was not scored in time
    assert not relevance.rerank_usable(gate, partial, 4, 3)
    assert [r for _, r in relevance.decide_vault(gate, items, 3, partial)] == [
        "cosine", "cosine", "below_floor", "below_floor"]
    assert relevance.vault_order(gate, 4, 3, partial) == [0, 1, 2]
    # Only as many as there are: two hits, both scored, is a usable turn.
    assert relevance.rerank_usable(gate, [0.4, 0.5], 2, 3)


# ------------------------------------------------------------------ prefetch

SCORES = {HITS[0]["text"]: 0.93, HITS[1]["text"]: 0.04, HITS[2]["text"]: 0.61, HITS[3]["text"]: 0.88}


def _app(tmp_path, state_db, gate=None, model=None, hits=HITS, **kw):
    reranker = _service(model if model is not None else _Fixed(SCORES), **kw)
    return sidecar.Quoth(ledger_path=tmp_path / "l.db", state_db=state_db, evidence=_ScoredEvidence(hits),
                         evidence_budget=5.0, gate=gate, reranker=reranker)


def test_rerank_floor_drops_what_cosine_kept(tmp_path, state_db):
    app = _app(tmp_path, state_db, Gate(vault_min_rerank=0.5))
    try:
        out = app.prefetch({"query": QUERY, "dry_run": True, "k_candidates": 4})
        assert out["evidence"] == 2 and "b.md:1-2" not in out["text"]
        vault = out["detail"]["candidates"]["vault"]
        assert [(c["rerank"], c["gate"], c["injected"]) for c in vault] == [
            (0.93, "rerank", True), (0.04, "rerank_floor", False), (0.61, "rerank", True), (None, "off", False)]
        assert out["detail"]["status"]["rerank"] == "ok" and "rerank" in out["detail"]["timings_ms"]
        assert app.health()["reranker"]["status"] == "ready"
        assert app.health()["relevance_gate"]["vault_min_rerank"] == 0.5
    finally:
        app.close()


def test_dry_runs_score_deeper_without_changing_what_is_injected(tmp_path, state_db):
    app = _app(tmp_path, state_db, Gate(vault_min_rerank=0.5))
    try:
        live = app.prefetch({"query": QUERY, "dry_run": True})
        deep = app.prefetch({"query": QUERY, "dry_run": True, "k_candidates": 4, "k_rerank": 4})
        assert live["text"] == deep["text"]
        assert [c["rerank"] for c in deep["detail"]["candidates"]["vault"]] == [0.93, 0.04, 0.61, 0.88]
    finally:
        app.close()


def test_widen_serves_the_best_by_rerank_among_the_top_n(tmp_path, state_db):
    app = _app(tmp_path, state_db, Gate(vault_min_rerank=0.5, vault_rerank_widen=4))
    try:
        out = app.prefetch({"query": QUERY, "dry_run": True, "k_candidates": 4})
        assert [e["locator"] for e in out["detail"]["evidence"]] == ["a.md:1-2", "d.md:1-2", "c.md:1-2"]
        vault = out["detail"]["candidates"]["vault"]
        # `served` keeps its meaning (the cosine prefix); rank 4 is injected through the widen.
        assert [c["served"] for c in vault] == [True, True, True, False]
        assert [c["injected"] for c in vault] == [True, False, True, True]
        assert out["text"].index("d.md:1-2") < out["text"].index("c.md:1-2")
    finally:
        app.close()


def test_score_only_mode_injects_exactly_what_no_reranker_does(tmp_path, state_db):
    plain = sidecar.Quoth(ledger_path=tmp_path / "p.db", state_db=state_db, evidence=_ScoredEvidence(HITS),
                          evidence_budget=5.0, gate=Gate(vault_min_cosine=0.5))
    scoring = _app(tmp_path, state_db, Gate(vault_min_cosine=0.5))
    try:
        for body in ({"query": QUERY}, {"query": QUERY, "dry_run": True, "k_candidates": 4}):
            a, b = plain.prefetch(dict(body)), scoring.prefetch(dict(body))
            assert a["text"] == b["text"] and a["evidence"] == b["evidence"]
        assert "rerank" not in json.dumps(plain.prefetch({"query": QUERY, "dry_run": True})["detail"])
        assert "reranker" not in plain.health()
        # Without a rerank threshold a live turn does not score (nor wait); dry runs do.
        model = scoring.reranker.model()
        model.calls.clear()
        scoring.prefetch({"query": QUERY})
        scoring.wait_idle()
        assert model.calls == []
        dry = scoring.prefetch({"query": QUERY, "dry_run": True})
        assert len(model.calls) == 3 and dry["detail"]["status"]["rerank"] == "ok"
    finally:
        plain.close()
        scoring.close()


@pytest.mark.parametrize("case", ["slow", "loading", "unavailable", "error"])
def test_turns_fall_back_to_the_cosine_gate(tmp_path, state_db, case):
    gate = Gate(vault_min_cosine=0.5, vault_min_rerank=0.5)
    release = threading.Event()
    if case == "slow":
        reranker = _service(_Fixed(SCORES, delay=0.4), budget=0.15)
    elif case == "loading":
        reranker = RerankService("hash-rerank", loader=lambda: release.wait(10) and _Fixed(SCORES)).start()
    elif case == "unavailable":
        reranker = RerankService("hash-rerank", loader=lambda: (_ for _ in ()).throw(OSError("missing")))
        reranker.wait()
    else:
        reranker = _service(_Fixed(SCORES, fail=True))
    app = sidecar.Quoth(ledger_path=tmp_path / "l.db", state_db=state_db, evidence=_ScoredEvidence(HITS),
                        evidence_budget=5.0, gate=gate, reranker=reranker)
    try:
        t0 = time.monotonic()
        out = app.prefetch({"query": QUERY, "dry_run": True, "k_candidates": 4})
        assert time.monotonic() - t0 < 1.5  # the turn never waits past the reranker's budget
        assert out["evidence_status"] == "ok" and out["detail"]["status"]["rerank"] == case
        vault = out["detail"]["candidates"]["vault"]
        assert [c["gate"] for c in vault] == ["cosine", "cosine", "below_floor", "below_floor"]
        assert out["evidence"] == 2 and "a.md:1-2" in out["text"] and "b.md:1-2" in out["text"]
    finally:
        release.set()
        app.wait_idle()
        app.close()


def test_claims_a_turn_serves_are_reranked(tmp_path, state_db):
    from quoth.ledger import core
    green, rose = "Alex prefers green tea in the morning.", "The garden has a tea rose."
    app = _app(tmp_path, state_db, Gate(claims_min_rerank=0.5), model=_Fixed({green: 0.9, rose: 0.05}),
               hits=[], claims=True)
    try:
        with app.lock:
            for text in (green, rose):
                cid = core.propose(app.conn, text=text, kind="preference", origin="assistant_proposal",
                                   proposer="t", reason="r")["claim"]["id"]
                core.accept(app.conn, cid, actor="t")
        out = app.prefetch({"query": "which tea do I drink", "dry_run": True})
        assert out["claims"] == 1 and green in out["text"] and rose not in out["text"]
        claims = {c["text"]: c for c in out["detail"]["candidates"]["claims"]}
        assert (claims[rose]["rerank"], claims[rose]["gate"]) == (0.05, "rerank_floor")
        assert claims[green]["gate"] == "rerank"
    finally:
        app.close()


def test_prefetch_log_carries_rerank_scores_without_text(tmp_path, state_db):
    from quoth.prefetch_log import PrefetchLog
    app = _app(tmp_path, state_db, Gate(vault_min_rerank=0.5))
    app.prefetch_log = PrefetchLog(tmp_path / "log", days=30)
    try:
        app.prefetch({"query": QUERY, "session_id": "s1"})
        app.prefetch_log.flush()
        raw = "".join(p.read_text() for p in (tmp_path / "log").glob("*.jsonl"))
        turn = json.loads(raw.splitlines()[0])
        assert [v["rerank"] for v in turn["stores"]["vault"]] == [0.93, 0.04, 0.61]
        assert turn["status"]["rerank"] == "ok" and "sourdough" not in raw
    finally:
        app.close()


def test_sidecar_cli_accepts_rerank_flags(monkeypatch):
    seen = {}
    monkeypatch.setattr(sidecar, "Quoth", lambda **kw: seen.update(kw) or (_ for _ in ()).throw(SystemExit(0)))
    with pytest.raises(SystemExit):
        sidecar.main(["--ledger", ":memory:", "--state-db", "", "--rerank-model", "hash-rerank",
                      "--vault-min-rerank", "0.3", "--vault-rerank-widen", "6"])
    assert seen["gate"] == Gate(vault_min_rerank=0.3, vault_rerank_widen=6)
    assert seen["reranker"].name == "hash-rerank" and not seen["reranker"].claims
    seen.clear()
    with pytest.raises(SystemExit):
        sidecar.main(["--ledger", ":memory:", "--state-db", "", "--vault-min-rerank", "0.3"])
    assert not seen  # refused before anything is built
    with pytest.raises(SystemExit):
        sidecar.main(["--ledger", ":memory:", "--state-db", ""])
    assert seen["reranker"] is None


# ------------------------------------------------------------------ sweep

def _item(score, rerank_score=None, served=True, match=()):
    out = {"score": score, "served": served, "injected": served}
    if rerank_score is not None:
        out["rerank"] = rerank_score
    if match:
        out["match"] = list(match)
    return out


def _rec(vault, expected=1, abstain=False):
    stores = {s: {"expected": 0, "items": []} for s in ("claims", "owner", "assistant")}
    stores["vault"] = {"expected": expected, "items": vault}
    return {"abstain": abstain, "answerable": not abstain, "has_stale": False, "stores": stores}


ROWS = {
    # Gold at rank 1; the cross-encoder sees the neighbours are noise.
    "a": _rec([_item(0.80, 0.97, match=[0]), _item(0.62, 0.02), _item(0.58, 0.01), _item(0.50, 0.3, served=False)]),
    # Gold at cosine rank 4, which only a widen can serve.
    "b": _rec([_item(0.66, 0.05), _item(0.61, 0.03), _item(0.60, 0.02), _item(0.59, 0.91, served=False, match=[0])]),
    # Nothing relevant, high cosines anyway.
    "c": _rec([_item(0.70, 0.01), _item(0.68, 0.004), _item(0.65, 0.002)], expected=0, abstain=True),
}


def test_sweep_replays_rerank_floors_and_widens():
    base, hits = SW.evaluate(ROWS, Gate())
    assert hits == {"a": True, "b": False} and base["abstain_nonempty_rate"] == 1.0
    gated, hits = SW.evaluate(ROWS, Gate(vault_min_rerank=0.1))
    assert hits == {"a": True, "b": False} and gated["injected_precision"] == 1.0
    assert gated["abstain_nonempty_rate"] == 0.0 and gated["vault_items_mean"] == round(1 / 3, 4)
    widened, hits = SW.evaluate(ROWS, Gate(vault_min_rerank=0.1, vault_rerank_widen=4))
    assert hits == {"a": True, "b": True} and widened["injected_hit_rate"] == 1.0
    # A row without scores keeps the cosine gate in the replay, as live.
    unscored = {"u": _rec([_item(0.70), _item(0.40)])}
    assert SW.evaluate(unscored, Gate(vault_min_cosine=0.5, vault_min_rerank=0.9))[0]["vault_items_mean"] == 1.0


def test_sweep_recommends_a_rerank_config_and_names_the_fallback():
    receipt = {"schema": "quoth-eval-run/v1", "set": {"sha256": "x"}, "code": {"version": "t"},
               "config": {"k_candidates": 4}, "candidates": {"schema": SW.CANDIDATES_SCHEMA, "rows": ROWS}}
    result = SW.sweep(receipt, vault_gaps=(None,), vault_rescues=(None,), claims_lexicals=(None,),
                      vault_rerank_widens=(None, 4, 5, 10))
    assert result["vault_reranked_share"] == 1.0
    assert {r["gate"].get("vault_rerank_widen") for r in result["vault_rerank"]} == {None, 4}  # 5, 10 > depth
    rec = result["recommended"]
    assert rec["gate"]["vault_rerank_widen"] == 4 and rec["injected_hit_rate"] == 1.0
    assert "--vault-rerank-widen 4" in result["flags"] and result["fallback_flags"] is not None
    text = SW.table(result)
    assert "Vault rerank floor only" in text and "Turns without rerank scores keep the cosine gate" in text
    # A receipt without rerank scores sweeps exactly as before.
    plain = {k: v for k, v in receipt.items()}
    plain["candidates"] = {"schema": SW.CANDIDATES_SCHEMA, "rows": {
        rid: _rec([{k: v for k, v in it.items() if k != "rerank"} for it in rec_["stores"]["vault"]["items"]],
                  rec_["stores"]["vault"]["expected"], rec_["abstain"]) for rid, rec_ in ROWS.items()}}
    result = SW.sweep(plain, vault_gaps=(None,), vault_rescues=(None,), claims_lexicals=(None,))
    assert result["vault_rerank"] == [] and result["vault_reranked_share"] is None
    assert "rerank" not in SW.table(result)


def test_record_keeps_rerank_scores_and_no_text():
    row = {"id": "r", "question": QUERY,
           "expect": {"abstain": False, "sources": [{"store": "vault", "match": {"path": "a.md"}}]}}
    cands = {"vault": [{"locator": "a.md:1-2", "path": "a.md", "score": 0.7, "served": True, "injected": True,
                        "lexical": 1.0, "matched": 3, "rerank": 0.97, "text": "Feed the sourdough starter."},
                       {"locator": "b.md:1-2", "path": "b.md", "score": 0.5, "served": True, "injected": False,
                        "lexical": 0.0, "matched": 0, "rerank": None, "text": "Rent."}]}
    a, b = SW.record(row, cands, row["expect"]["sources"])["stores"]["vault"]["items"]
    assert a["rerank"] == 0.97 and "rerank" not in b and "Rent" not in json.dumps([a, b])


def test_rerank_replay_matches_a_live_gated_run(built):  # noqa: F811
    """Score once (no rerank gate), replay rerank configs, and get exactly what an in-process
    sidecar with those flags injects."""
    from quoth.evals.sweep import evaluate
    scoring = InProcessTarget.build(**built["paths"], model=built["model"], episodes_prefetch=True,
                                    reranker=RerankService("hash-rerank", claims=True))
    try:
        receipt, _ = run_set(load_set(SET), scoring, gold_drift=True)
    finally:
        scoring.close()
    assert receipt.data["config"]["rerank"]["model"] == "hash-rerank"
    assert receipt.data["statuses"]["rerank"] == {"ok": len(load_set(SET).rows)}
    rows = receipt.data["candidates"]["rows"]
    assert all("rerank" in it for rec in rows.values() for it in rec["stores"]["vault"]["items"])
    for gate in (Gate(vault_min_rerank=0.3, claims_min_rerank=0.3),
                 Gate(vault_min_rerank=0.2, vault_max_rerank_gap=0.4, vault_rerank_widen=6)):
        replayed, _ = evaluate(rows, gate)
        target = InProcessTarget.build(**built["paths"], model=built["model"], episodes_prefetch=True, gate=gate,
                                       reranker=RerankService("hash-rerank", claims=True))
        try:
            gated, _ = run_set(load_set(SET), target, gold_drift=True)
        finally:
            target.close()
        overall = gated.data["metrics"]["overall"]
        for key in ("injected_hit_rate", "injected_mrr", "injected_precision", "abstain_nonempty_rate",
                    "empty_rate", "stale_leak"):
            assert replayed[key] == overall[key], (gate, key)
        assert replayed["items_mean"] != evaluate(rows, Gate())[0]["items_mean"]  # the gate did something


def test_eval_cli_rerank_flags(built, tmp_path, capsys):  # noqa: F811
    from pathlib import Path
    from quoth.cli import main as cli_main
    fixture_dir = str(Path(built["paths"]["db"]).parent)
    out = tmp_path / "r.json"
    assert cli_main(["eval", "run", str(SET), "--fixture-dir", fixture_dir, "--rerank-model", "hash-rerank",
                     "--out", str(out), "--quiet"]) == 0
    assert cli_main(["eval", "sweep", str(out), "--vault-reranks", "0.1,0.3", "--vault-rerank-gaps", "none",
                     "--vault-rerank-widens", "none,5"]) == 0
    assert "Vault rerank floor only" in capsys.readouterr().out
    assert cli_main(["eval", "run", str(SET), "--fixture-dir", fixture_dir, "--vault-min-rerank", "0.3",
                     "--quiet"]) == 2
    assert "--rerank-model" in capsys.readouterr().err
    assert cli_main(["eval", "run", str(SET), "--socket", str(tmp_path / "s.sock"), "--rerank-model",
                     "hash-rerank", "--quiet"]) == 2
    assert "in-process only" in capsys.readouterr().err


# ------------------------------------------------------------------ review fixes (0.13.2)

def test_back_to_back_turns_keep_vault_evidence_during_a_slow_rerank(tmp_path, state_db):
    """Scoring has its own worker: the next turn's vault evidence never waits behind it; the
    turn finding the reranker busy skips it (cosine gate), and a late job stops between pairs."""
    model = _Fixed(SCORES, delay=0.4)  # pair 2 runs 0.4-0.8 s, past the 0.5 s budget
    app = _app(tmp_path, state_db, Gate(vault_min_cosine=0.5, vault_min_rerank=0.5), model=model, budget=0.5)
    try:
        first = app.prefetch({"query": QUERY, "dry_run": True})
        assert first["evidence_status"] == "ok" and first["detail"]["status"]["rerank"] == "slow"
        second = app.prefetch({"query": QUERY, "dry_run": True})
        assert second["evidence_status"] == "ok" and second["detail"]["status"]["rerank"] == "busy"
        assert second["evidence"] == 2 and "a.md:1-2" in second["text"]  # cosine gate, hits kept
        live = app.prefetch({"query": QUERY})
        assert live["evidence_status"] == "ok" and live["evidence"] == 2
        app.wait_idle()
        # The first job was cancelled at its budget: its third pair never started.
        assert model.calls.count(HITS[2]["text"]) == 0
        assert app.prefetch({"query": QUERY, "dry_run": True})["evidence_status"] == "ok"
    finally:
        app.wait_idle()
        app.close()


def test_late_claims_cost_only_the_claims_rerank(tmp_path, state_db):
    from quoth.ledger import core
    green = "Alex prefers green tea in the morning."
    model = _Fixed(SCORES, delays={green: 0.6})
    app = _app(tmp_path, state_db, Gate(vault_min_rerank=0.5, claims_min_rerank=0.5), model=model,
               budget=0.3, claims=True)
    try:
        with app.lock:
            cid = core.propose(app.conn, text=green, kind="preference", origin="assistant_proposal",
                               proposer="t", reason="r")["claim"]["id"]
            core.accept(app.conn, cid, actor="t")
        out = app.prefetch({"query": "how do I feed the sourdough starter with green tea", "dry_run": True})
        assert out["detail"]["status"]["rerank"] == "partial"
        vault = out["detail"]["candidates"]["vault"]
        assert [c["gate"] for c in vault] == ["rerank", "rerank_floor", "rerank"]  # vault scores kept
        (claim,) = out["detail"]["candidates"]["claims"]
        assert claim["rerank"] is None and claim["gate"] == "off" and claim["injected"]  # claim rules
    finally:
        app.wait_idle()
        app.close()


def test_only_the_claims_a_turn_serves_are_scored(tmp_path, state_db, monkeypatch):
    import numpy as np
    from quoth.ledger import core
    green, pending = "Alex prefers green tea in the morning.", "Alex might like oolong tea."
    model = _Fixed({green: 0.9, pending: 0.9})
    app = _app(tmp_path, state_db, Gate(claims_min_rerank=0.5), model=model, hits=[], claims=True)
    try:
        with app.lock:
            gid = core.propose(app.conn, text=green, kind="preference", origin="assistant_proposal",
                               proposer="t", reason="r")["claim"]["id"]
            core.accept(app.conn, gid, actor="t")
            pid = core.propose(app.conn, text=pending, kind="preference", origin="assistant_proposal",
                               proposer="t", reason="r")["claim"]["id"]  # pending: never served
        for cid, text in ((gid, green), (pid, pending)):  # a stale index still holds the pending one
            app.claim_index._vectors[cid] = (text, np.ones(4, dtype=np.float32))
        monkeypatch.setattr(app, "_dense_work",
                            lambda q, k, k_claims: {"hits": [], "claims": [(pid, 0.9), (gid, 0.8)]})
        out = app.prefetch({"query": "which tea", "dry_run": True})
        assert green in model.calls and pending not in model.calls
        assert out["claims"] == 1 and green in out["text"]
    finally:
        app.close()


def test_a_failing_recall_never_leaves_the_worker_waiting(tmp_path, state_db, monkeypatch):
    from quoth.ledger import core as ledger_core
    app = _app(tmp_path, state_db, Gate(vault_min_rerank=0.5), claims=True)
    try:
        monkeypatch.setattr(ledger_core, "recall", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")))
        with pytest.raises(RuntimeError):
            app.prefetch({"query": QUERY})
        monkeypatch.undo()
        t0 = time.monotonic()
        app.wait_idle(timeout=5)
        assert time.monotonic() - t0 < 1.0
        assert app.prefetch({"query": QUERY, "dry_run": True})["detail"]["status"]["rerank"] == "ok"
    finally:
        app.close()


def test_pinned_checksums_are_verified(tmp_path):
    import hashlib
    from quoth.rerank import RerankSpec
    int8 = dict(rerank.MODELS["bge-reranker-v2-m3-int8"].sha256)
    assert int8["onnx/model_int8.onnx"] == "912fc1215c2dbff6499700534bd8d31253af01573861abbfc43afd1fab6cce5d"
    assert int8["tokenizer.json"] == "8bf8afbfd11306bd872018c53bfdf2e160a56f8edbcf49933324404791c148d3"
    fp32 = dict(rerank.MODELS["bge-reranker-v2-m3"].sha256)
    assert fp32["onnx/model.onnx_data"] == "f009aa6c6cf21986fd7e0021fa66b20ccce27abc6900a57c7109c8496811bcbe"
    assert all(len(d) == 64 for spec in rerank.MODELS.values() for _, d in spec.sha256)
    (tmp_path / "a.bin").write_bytes(b"weights")
    spec = RerankSpec("x", "onnx", sha256=(("a.bin", hashlib.sha256(b"weights").hexdigest()),))
    rerank.verify(spec, str(tmp_path))
    (tmp_path / "a.bin").write_bytes(b"tampered")
    with pytest.raises(rerank.RerankerUnavailable, match="a.bin"):
        rerank.verify(spec, str(tmp_path))
    svc = RerankService("hash-rerank", loader=lambda: rerank.verify(spec, str(tmp_path)))
    assert svc.wait() == "unavailable" and svc.model() is None


def test_a_tampered_model_is_refused_after_download_and_on_load(tmp_path, monkeypatch):
    pytest.importorskip("onnxruntime")
    pytest.importorskip("tokenizers")
    from quoth import models
    spec = rerank.MODELS[rerank.DEFAULT_MODEL]

    def fake_download(url, part):
        assert f"/resolve/{rerank.REVISION}/" in url
        part.write_bytes(b"not the pinned file")
    monkeypatch.setattr(models, "_download", fake_download)
    with pytest.raises(models.ModelError, match="does not match the pin"):
        models.fetch(models.reranker_pin(spec.name), tmp_path)
    for f in spec.files:  # a copy that skipped the fetch check is still refused on load
        path = tmp_path / spec.repo / f
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not the pinned file")
    with pytest.raises(rerank.RerankerUnavailable, match="checksum mismatch"):
        rerank.get_reranker(rerank.DEFAULT_MODEL, cache_dir=str(tmp_path))
    assert RerankService(rerank.DEFAULT_MODEL, cache_dir=str(tmp_path)).wait() == "unavailable"


def test_the_reranker_loads_after_the_embedder_warmup():
    order = []

    class _Evidence:
        def warm(self, models):
            time.sleep(0.05)
            order.append("warm")
            raise RuntimeError("cold")  # a failed warmup still starts the reranker

    svc = RerankService("hash-rerank", loader=lambda: order.append("load") or _Fixed({}))
    with pytest.raises(RuntimeError):
        sidecar._warm_then_rerank(_Evidence(), "bge-m3", svc)
    assert svc.wait() == "ready" and order == ["warm", "load"]


def _timed(rows: dict, ms: dict) -> dict:
    out = {}
    for rid, rec in rows.items():
        items = [{**it, "rerank_ms": ms[rid]} if "rerank" in it else it for it in rec["stores"]["vault"]["items"]]
        out[rid] = {**rec, "stores": {**rec["stores"], "vault": {**rec["stores"]["vault"], "items": items}}}
    return out


def test_sweep_replays_the_live_rerank_budget():
    rows = _timed(ROWS, {"a": 100.0, "b": 400.0, "c": 50.0})  # b: 3 pairs take 1.2 s
    gate = Gate(vault_min_rerank=0.1)
    free, _ = SW.evaluate(rows, gate)
    timed, _ = SW.evaluate(rows, gate, budget_ms=800)
    assert free["vault_items_mean"] == round(1 / 3, 4)
    # Row b is not scored in time live: no gate on its vault (cosine off), 3 hits injected.
    assert timed["vault_items_mean"] == round(4 / 3, 4)
    assert timed["rerank_in_time"] == round(2 / 3, 4) and timed["rerank_p95_ms"] == 1200.0
    receipt = {"schema": "quoth-eval-run/v1", "set": {"sha256": "x"}, "code": {"version": "t"},
               "config": {"k_candidates": 4}, "candidates": {"schema": SW.CANDIDATES_SCHEMA, "rows": rows}}
    tight = SW.sweep(receipt, vault_gaps=(None,), vault_rescues=(None,), claims_lexicals=(None,))
    assert tight["vault_rerank"] and not any(r["eligible"] for r in tight["vault_rerank"])
    assert all(r["over_budget"] for r in tight["vault_rerank"]) and tight["rerank_in_time"] == round(2 / 3, 4)
    assert tight["recommended"] is None or not Gate.from_dict(tight["recommended"]["gate"]).rerank_on
    text = SW.table(tight)
    assert "p95 ms" in text and "rerank budget" in text
    loose = SW.sweep(receipt, vault_gaps=(None,), vault_rescues=(None,), claims_lexicals=(None,),
                     rerank_budget=2.0)
    assert any(r["eligible"] for r in loose["vault_rerank"]) and loose["rerank_in_time"] == 1.0


def test_sweep_scoring_also_ends_at_the_evidence_deadline():
    rows = _timed(ROWS, {"a": 100.0, "b": 100.0, "c": 100.0})  # 300 ms per row, inside 0.8 s
    rows["a"] = {**rows["a"], "dense_ms": 1300.0}  # the search left 200 ms of the 1.5 s budget
    gate = Gate(vault_min_rerank=0.1)
    fitted, spent, limit = SW.fit(gate, rows["a"], 800.0)
    assert (spent, limit) == (300.0, 200.0) and not any("rerank" in it for it in fitted["stores"]["vault"]["items"])
    assert SW.fit(gate, rows["b"], 800.0)[2] == 800.0
    out, _ = SW.evaluate(rows, gate, budget_ms=800)
    assert out["rerank_in_time"] == round(2 / 3, 4) and out["rerank_p95_over_ms"] == 100.0
    assert SW.evaluate(rows, gate, budget_ms=800, evidence_ms=None)[0]["rerank_in_time"] == 1.0
    # eval run records the row's search time when it reranked.
    row = {"id": "r", "question": QUERY, "expect": {"abstain": False, "sources": []}}
    cands = {"vault": [{"locator": "a.md:1-2", "score": 0.7, "served": True, "injected": True, "rerank": 0.9,
                        "rerank_ms": 40.0}]}
    assert SW.record(row, cands, [], timings={"vault": 120.0, "claims_dense": 5.5})["dense_ms"] == 125.5
    assert "dense_ms" not in SW.record(row, {"vault": [{"locator": "a", "score": 0.7}]}, [], timings={"vault": 1.0})


@pytest.mark.parametrize("with_rerank", [True, False])
def test_a_claim_forgotten_mid_turn_does_not_break_prefetch(tmp_path, state_db, monkeypatch, with_rerank):
    """Another session forgets a served dense claim while this turn scores (or gates): the
    turn still answers with its vault evidence."""
    import numpy as np
    from quoth.ledger import core
    text = "Rye levain hydration stays at seventy percent."  # meaning match only: no shared word
    plain = not with_rerank
    gate = Gate(vault_min_rerank=0.5, claims_min_rerank=0.5) if with_rerank else Gate(vault_min_cosine=0.5)
    app = _app(tmp_path, state_db, gate, claims=True) if with_rerank else sidecar.Quoth(
        ledger_path=tmp_path / "l.db", state_db=state_db, evidence=_ScoredEvidence(HITS), evidence_budget=5.0,
        gate=gate)
    try:
        with app.lock:
            cid = core.propose(app.conn, text=text, kind="preference", origin="assistant_proposal",
                               proposer="t", reason="r")["claim"]["id"]
            core.accept(app.conn, cid, actor="t")
        app.claim_index._vectors[cid] = (text, np.ones(4, dtype=np.float32))
        monkeypatch.setattr(app, "_dense_work",
                            lambda q, k, k_claims: {"hits": [dict(h) for h in HITS[:k]], "claims": [(cid, 0.8)]})

        forgotten = []

        def forget():
            if not forgotten:
                forgotten.append(cid)
                with app.lock:
                    core.forget(app.conn, cid, reason="another session")

        if plain:  # no scoring window: forget between the served read and the claim gate
            real = relevance.judge_vault
            monkeypatch.setattr(relevance, "judge_vault", lambda *a, **k: (forget(), real(*a, **k))[1])
        else:
            model = app.reranker.model()
            real_score = model.score
            model.score = lambda q, texts, deadline=None: (forget(),
                                                           real_score(q, texts, deadline))[1]
        out = app.prefetch({"query": "how do I feed the sourdough starter", "dry_run": True})
        assert out["evidence_status"] == "ok" and out["evidence"] >= 1
        live = app.prefetch({"query": "how do I feed the sourdough starter"})
        assert live["evidence_status"] == "ok"
    finally:
        app.wait_idle()
        app.close()


def test_a_new_search_cancels_the_running_rerank(tmp_path, state_db):
    model = _Fixed(SCORES, delay=0.4)
    app = _app(tmp_path, state_db, Gate(vault_min_rerank=0.5), model=model, budget=5.0)
    try:
        result = {}

        def first():
            t0 = time.monotonic()
            result["out"] = app.prefetch({"query": QUERY, "dry_run": True})
            result["s"] = time.monotonic() - t0

        th = threading.Thread(target=first)
        th.start()
        time.sleep(0.15)  # the first pair is being scored
        second = app.prefetch({"query": QUERY, "dry_run": True})
        th.join(5)
        assert second["evidence_status"] == "ok"
        # The first turn's job stopped before its next pair instead of running 3 x 0.4 s.
        assert result["out"]["detail"]["status"]["rerank"] in ("slow", "partial") and result["s"] < 1.0
        app.wait_idle()
        assert len(model.calls) <= 2
    finally:
        app.wait_idle()
        app.close()


def test_close_cancels_a_long_rerank_job(tmp_path, state_db):
    model = _Fixed(SCORES, delay=0.3)
    app = _app(tmp_path, state_db, Gate(vault_min_rerank=0.5), model=model, budget=30.0)
    errors = []

    def turn():
        try:
            app.prefetch({"query": QUERY, "dry_run": True, "k_candidates": 4, "k_rerank": 4})
        except Exception as exc:  # the ledger closes under it; only the job's stop matters here
            errors.append(exc)

    th = threading.Thread(target=turn)
    th.start()
    time.sleep(0.1)
    cancel, job = app._rerank_cancel, app._rerank_future
    app.close()
    assert cancel is not None and cancel.is_set()
    job.result(timeout=2)  # stops after the pair in flight, not 4 x 0.3 s later
    th.join(5)
    assert len(model.calls) <= 2
