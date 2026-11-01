"""quoth.evals: eval set v1, converters, the prefetch harness on the public fixture,
receipts (no text), compare, online readout and gate-metric parity."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from quoth.cli import main as cli_main
from quoth.evals import (InProcessTarget, KINDS, ORIGINS, Receipt, STORES, compare, load_set,
                             run_set, validate)
from quoth.evals.convert import (ConvertError, convert, from_golden_v1, from_golden_v3,
                                     to_golden_v1, to_golden_v3)
from quoth.evals.fixture import build_fixture, build_ledger, build_snapshot
from quoth.evals.format import MATCH_KEYS, row_errors, vault_paths, write_set

from conftest import write_vault

REPO = Path(__file__).resolve().parents[1]
PUBLIC = REPO / "eval" / "public"
SET = PUBLIC / "set.jsonl"


# ------------------------------------------------------------------ format

def test_public_set_is_valid_and_covers_every_kind():
    assert validate(SET) == []
    evalset = load_set(SET)
    assert evalset.manifest["privacy"] == "public" and evalset.manifest["fixture"] == "eval/public"
    assert set(evalset.manifest["counts"]["kind"]) == set(KINDS)
    assert {"en", "ru"} <= set(evalset.manifest["counts"]["lang"])
    assert any(r.get("context") for r in evalset.rows if r["kind"] == "followup")
    assert any(r["expect"].get("stale") for r in evalset.rows if r["kind"] == "knowledge_update")
    assert 20 <= len(evalset.rows) <= 40


def test_json_schema_matches_the_validator():
    schema = json.loads((REPO / "eval" / "schema" / "evalset-v2.schema.json").read_text())
    props = schema["properties"]
    assert tuple(props["kind"]["enum"]) == KINDS and tuple(props["origin"]["enum"]) == ORIGINS
    assert tuple(schema["$defs"]["source"]["properties"]["store"]["enum"]) == STORES
    assert set(schema["$defs"]["source"]["properties"]["match"]["properties"]) == set(MATCH_KEYS)
    assert set(schema["required"]) == {"id", "question", "lang", "kind", "origin", "expect"}


def _row(**kw):
    row = {"id": "r1", "question": "a private question text", "lang": "en", "kind": "fact",
           "origin": "synthetic", "expect": {"abstain": False,
                                             "sources": [{"store": "vault", "match": {"path": "a.md"}}]}}
    row.update(kw)
    return row


@pytest.mark.parametrize("bad", [
    {"kind": "nope"}, {"origin": "robot"}, {"lang": "English"}, {"asked_at": "yesterday"},
    {"expect": {"abstain": True, "sources": [{"store": "vault", "match": {"path": "a.md"}}]}},
    {"expect": {"abstain": False, "sources": []}},
    {"expect": {"abstain": False, "sources": [{"store": "claims", "match": {"path": "a.md"}}]}},
    {"expect": {"abstain": False, "sources": [{"store": "vault", "match": {"path": "../x.md"}}]}},
    {"kind": "unanswerable"}, {"kind": "followup"},
    {"context": [{"role": "system", "text": "x"}]}, {"extra": 1},
])
def test_invalid_rows_are_named_by_id_without_text(bad):
    errors = row_errors(_row(**bad))
    assert errors and all(e.startswith("r1:") for e in errors)
    assert not any("private question" in e for e in errors)


def test_manifest_must_match_the_rows(tmp_path):
    out = tmp_path / "s.jsonl"
    write_set([_row()], out, privacy="private", fixture="private")
    assert validate(out) == []
    assert oct(out.stat().st_mode & 0o777) == "0o600"
    out.write_text(out.read_text().replace("a.md", "b.md"))
    assert "manifest sha256 does not match the set" in validate(out)
    with pytest.raises(ValueError):
        write_set([_row(expect={"abstain": False, "answer_hint": "secret",
                                "sources": [{"store": "vault", "match": {"path": "a.md"}}]})],
                  tmp_path / "p.jsonl", privacy="public", fixture="x")


# ------------------------------------------------------------------ converters

V1 = [
    {"id": "g1", "question": "q one", "lang": "en", "expected_paths": ["A/x.md", "B/y.md"],
     "scope": "general", "kind": "fact"},
    {"id": "g2", "question": "q two", "lang": "ru", "expected_paths": [], "scope": "general",
     "kind": "adversarial"},
    {"id": "g3", "question": "q three", "lang": "en", "expected_paths": ["C/z.md"], "scope": "work",
     "kind": "project"},
    {"id": "g4", "question": "q four", "lang": "en", "expected_paths": ["D.md"], "scope": "general",
     "kind": "something-new"},
]


def _v3(i, kind="fact", origin="authored", paths=("N/a.md",), event=None):
    return {"id": f"v{i}", "question": f"synthetic question {i}", "lang": "ru", "kind": kind,
            "domain": "synthetic", "expected_paths": list(paths), "answer_hint": f"hint {i}",
            "abstain": not paths, "post_snapshot": False, "asked_at": "2026-09-01",
            "source_kind": "authored" if origin == "authored" else "telegram", "origin": origin,
            "temporal_kind": "none" if event is None else "on_date", "expected_event": event}


V3 = [_v3(1), _v3(2, "project-state", "real_user"), _v3(3, "unanswerable", "assistant_query", paths=()),
      _v3(4, "temporal", "authored", event={"date": "2026-01-01", "note_path": "N/a.md",
                                             "line_span": [1, 2], "precision": "day", "status": "done"})]


def test_golden_v1_round_trip_keeps_expected_paths():
    rows = from_golden_v1(V1)
    assert all(row_errors(r) == [] for r in rows)
    assert [r["kind"] for r in rows] == ["fact", "unanswerable", "project_state", "fact"]
    assert rows[1]["expect"]["abstain"] is True
    assert [to_golden_v1(r) for r in rows] == V1


def test_golden_v3_round_trip_is_exact():
    rows = from_golden_v3(V3)
    assert all(row_errors(r) == [] for r in rows)
    assert [r["origin"] for r in rows] == ["authored", "real_user", "assistant_query", "authored"]
    assert rows[1]["kind"] == "project_state" and rows[2]["expect"]["abstain"] is True
    assert [to_golden_v3(r) for r in rows] == V3
    assert [vault_paths(r) for r in rows] == [r["expected_paths"] for r in V3]


def test_convert_cli_writes_set_and_manifest_and_checks_package(tmp_path, capsys):
    src = tmp_path / "pkg" / "real-questions-v3.jsonl"
    src.parent.mkdir()
    src.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in V3))
    out = tmp_path / "v3.jsonl"
    assert cli_main(["eval", "convert", "--from", "golden-v3", str(src), str(out)]) == 0
    printed = capsys.readouterr().out
    assert "synthetic question" not in printed and "hint" not in printed
    assert validate(out) == [] and load_set(out).manifest["privacy"] == "private"
    assert load_set(out).manifest["source"]["format"] == "golden-v3"
    # A package manifest next to the input pins its hash.
    (src.parent / "manifest.json").write_text(json.dumps(
        {"schema": "quoth-private-golden-package/v1", "dataset": {"sha256": "0" * 64}}))
    with pytest.raises(ConvertError):
        convert("golden-v3", src, tmp_path / "again.jsonl")
    assert cli_main(["eval", "convert", "--from", "golden-v3", str(src), str(tmp_path / "x.jsonl")]) == 2


def test_convert_ledger_updates_writes_rows_and_seed(tmp_path):
    src = tmp_path / "updates.jsonl"
    src.write_text(json.dumps({"id": "u1", "steps": [
        {"text": "The kettle is red.", "kind": "environment", "subject_key": "kettle"},
        {"text": "The kettle is blue now.", "kind": "environment", "subject_key": "kettle"}],
        "query": "kettle colour", "current": "The kettle is blue now.",
        "stale": ["The kettle is red."]}) + "\n")
    summary = convert("ledger-updates", src, tmp_path / "ku.jsonl", privacy="public", fixture="t")
    assert summary["rows"] == 1 and summary["ledger_seed_rows"] == 2
    row = load_set(tmp_path / "ku.jsonl").rows[0]
    assert row["kind"] == "knowledge_update" and row["expect"]["stale"]
    seed = [json.loads(line) for line in (tmp_path / "ku.ledger-seed.jsonl").read_text().splitlines()]
    assert [s["status"] for s in seed] == ["accepted", "accepted"]


# ------------------------------------------------------------------ harness on the public fixture

@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("public-fixture")
    return build_fixture(PUBLIC, out)


@pytest.fixture(scope="module")
def run(built):
    # No gate (hash-ngram cosines sit under the bge-m3 serving floor): the sweep's baseline.
    from quoth.relevance import Gate
    # Dense ranking: the parity oracles below are the dense v0.11.0 evaluator.
    target = InProcessTarget.build(**built["paths"], model=built["model"], episodes_prefetch=True, gate=Gate(),
                                   retrieval="dense")
    try:
        receipt, detail = run_set(load_set(SET), target, gold_drift=True, detail=True)
    finally:
        target.close()
    return receipt, detail


def test_public_fixture_runs_end_to_end_with_receipt_shape(run):
    receipt, detail = run
    data = receipt.data
    assert data["schema"] == "quoth-eval-run/v1" and data["status"] == "ok"
    assert data["set"]["sha256"] == load_set(SET).sha256 and data["set"]["privacy"] == "public"
    assert data["config"]["mode"] == "in_process" and data["config"]["model"] == "hash-ngram"
    assert all(data["config"]["stores"].values())
    assert data["scored"] == len(load_set(SET).rows) and data["gold_drift"]["stale"] == 0
    metrics = data["metrics"]
    assert set(metrics) == {"overall", "ci95", "by_kind", "by_origin", "by_lang"}
    assert set(metrics["by_kind"]) == set(KINDS)
    overall = metrics["overall"]
    for key in ("injected_hit_rate", "injected_mrr", "injected_precision", "abstain_nonempty_rate",
                "stale_leak", "empty_rate"):
        assert key in overall
    assert set(overall["stores"]) == set(STORES)
    for store in STORES:
        st = overall["stores"][store]
        assert st["n"] > 0
        for k in (1, 3, 5, 10):
            assert 0.0 <= st[f"hit@{k}"] <= 1.0 and 0.0 <= st[f"recall@{k}"] <= 1.0
    for stage in ("total", "wall", "claims_lexical", "vault", "owner", "assistant"):
        assert data["latency_ms"][stage]["n"] == data["scored"]
    assert set(data["items"]["vault_hit@10"]) <= {r["id"] for r in load_set(SET).rows}
    assert len(detail) == data["scored"] and all("candidates" in d for d in detail)


def test_receipt_never_contains_row_content(run):
    receipt, _ = run
    blob = receipt.to_json()
    for row in load_set(SET).rows:
        assert row["question"] not in blob
        for path in vault_paths(row):
            assert path not in blob
        for src in row["expect"]["sources"]:
            (value,) = src["match"].values()
            assert value not in blob


def test_knowledge_updates_never_leak_stale_claims(run):
    receipt, _ = run
    ku = receipt.data["metrics"]["by_kind"]["knowledge_update"]
    assert ku["n"] == 6 and ku["stale_leak"] == 0
    assert ku["stores"]["claims"]["hit@1"] == 1.0


def test_dry_run_candidates_do_not_change_what_is_injected(built):
    target = InProcessTarget.build(**built["paths"], model=built["model"], episodes_prefetch=True)
    try:
        target.prepare()
        for row in load_set(SET).rows:
            plain = target.prefetch({"query": row["question"], "dry_run": True})
            deep = target.prefetch({"query": row["question"], "dry_run": True, "k_candidates": 20})
            assert plain["text"] == deep["text"], row["id"]
            injected = {s: [c for c in v if c["injected"]] for s, v in deep["detail"]["candidates"].items()}
            assert len(injected["vault"]) == plain["evidence"] and len(injected["claims"]) == plain["claims"]
    finally:
        target.close()


def test_cli_run_validate_compare(built, tmp_path, capsys):
    fixture_dir = str(Path(built["paths"]["db"]).parent)
    a, b, detail = tmp_path / "a.json", tmp_path / "b.json", tmp_path / "detail.jsonl"
    assert cli_main(["eval", "validate", str(SET)]) == 0
    assert cli_main(["eval", "run", str(SET), "--fixture-dir", fixture_dir, "--out", str(a),
                     "--detail-out", str(detail), "--quiet"]) == 0
    captured = capsys.readouterr()
    assert "private" in captured.err and oct(detail.stat().st_mode & 0o777) == "0o600"
    assert cli_main(["eval", "run", str(SET), "--fixture-dir", fixture_dir, "--out", str(b), "--quiet"]) == 0
    capsys.readouterr()
    assert cli_main(["eval", "compare", str(a), str(b), "--tolerances", str(_loose(tmp_path))]) == 0
    assert json.loads(capsys.readouterr().out)["result"] == "pass"
    # A lost gold item and a metric drop fail the gate (exit 1).
    worse = json.loads(b.read_text())
    lost = next(i for i, hit in worse["items"]["vault_hit@10"].items() if hit)
    worse["items"]["vault_hit@10"][lost] = False
    worse["metrics"]["overall"]["stores"]["vault"]["hit@10"] -= 0.1
    b.write_text(json.dumps(worse))
    assert cli_main(["eval", "compare", str(a), str(b)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["lost_gold"] == [lost] and result["result"] == "fail"
    # Different sets are not comparable (exit 3); unreadable input is exit 2.
    worse["set"]["sha256"] = "0" * 64
    b.write_text(json.dumps(worse))
    assert cli_main(["eval", "compare", str(a), str(b)]) == 3
    assert cli_main(["eval", "compare", str(a), str(tmp_path / "missing.json")]) == 2


def _loose(tmp_path):
    path = tmp_path / "tolerances.json"
    path.write_text(json.dumps({"metrics": {"metrics.overall.injected_hit_rate": {"max_drop": 0.0},
                                            "latency_ms.wall.p95": {"max_rise_ratio": 100}}}))
    return path


# ------------------------------------------------------------------ relevance gate sweep

GATE = {"vault_min_cosine": 0.2, "vault_max_gap": 0.06, "vault_lexical_rescue": 0.5,
        "claims_min_cosine": None, "claims_min_lexical": 0.34}
REPLAYED = ("injected_hit_rate", "injected_mrr", "injected_precision", "abstain_nonempty_rate",
            "empty_rate", "stale_leak")


def test_receipt_records_candidate_signals_without_text(run):
    receipt, _ = run
    cands = receipt.data["candidates"]
    assert cands["schema"] == "quoth-eval-candidates/v1" and set(cands["rows"]) == {
        r["id"] for r in load_set(SET).rows}
    vault = [it for rec in cands["rows"].values() for it in rec["stores"]["vault"]["items"]]
    assert vault and all(isinstance(it["score"], float) and "lexical" in it for it in vault)
    assert receipt.data["config"]["gate"] == {k: None for k in GATE}


def test_eval_run_gates_with_the_serving_defaults(built):
    """Without gate flags `eval run` judges exactly as the sidecar serves (`recall_defaults.GATE`)."""
    from quoth import recall_defaults
    from quoth.relevance import Gate
    target = InProcessTarget.build(**built["paths"], model=built["model"])
    try:
        assert target.config()["gate"] == Gate(**recall_defaults.GATE).to_dict()
        assert target.config()["gate"]["vault_min_cosine"] == recall_defaults.GATE["vault_min_cosine"]
    finally:
        target.close()


def test_sweep_baseline_reproduces_the_receipt(run):
    from quoth.evals.sweep import sweep
    receipt, _ = run
    result = sweep(receipt.data)
    overall, base = receipt.data["metrics"]["overall"], result["baseline"]
    for key in REPLAYED:
        assert base[key] == overall[key], key
    for store in ("vault", "claims"):
        for k in ("hit@5", "recall@10"):
            assert base[f"{store}_{k}"] == overall["stores"][store][k]
    assert result["vault_scored_share"] == 1.0 and result["recommended"]["eligible"]
    assert result["recommended"]["injected_precision"] > base["injected_precision"]


def test_sweep_replay_matches_a_live_gated_run(built, run):
    """A config replayed by the sweep scores exactly what the sidecar does with those flags."""
    from quoth.evals.sweep import evaluate
    from quoth.relevance import Gate
    receipt, _ = run
    replayed, _ = evaluate(receipt.data["candidates"]["rows"], Gate.from_dict(GATE))
    target = InProcessTarget.build(**built["paths"], model=built["model"], episodes_prefetch=True,
                                   gate=Gate.from_dict(GATE), retrieval="dense")
    try:
        gated, _ = run_set(load_set(SET), target, gold_drift=True)
    finally:
        target.close()
    assert gated.data["config"]["gate"] == GATE
    overall = gated.data["metrics"]["overall"]
    for key in REPLAYED:
        assert replayed[key] == overall[key], key
    assert overall["abstain_nonempty_rate"] < receipt.data["metrics"]["overall"]["abstain_nonempty_rate"]


def test_cli_sweep_and_old_receipts(built, tmp_path, capsys):
    fixture_dir = str(Path(built["paths"]["db"]).parent)
    a, out = tmp_path / "a.json", tmp_path / "sweep.json"
    assert cli_main(["eval", "run", str(SET), "--fixture-dir", fixture_dir, "--out", str(a), "--quiet"]) == 0
    assert cli_main(["eval", "sweep", str(a), "--out", str(out), "--vault-gaps", "none,0.05"]) == 0
    table = capsys.readouterr().out
    assert "| no gate |" in table and "Recommended:" in table
    result = json.loads(out.read_text())
    assert result["schema"] == "quoth-eval-sweep/v1" and {r["gate"]["vault_max_gap"] for r in result["vault"]} == {
        None, 0.05}
    for row in load_set(SET).rows:  # configs and aggregates only
        assert row["question"] not in out.read_text() and row["id"] not in table
    # A receipt from before 0.12.5 still loads and compares; the sweep names what it lacks.
    old = json.loads(a.read_text())
    del old["candidates"], old["config"]["gate"]
    b = tmp_path / "old.json"
    b.write_text(json.dumps(old))
    assert cli_main(["eval", "compare", str(b), str(a), "--tolerances", str(_loose(tmp_path))]) == 0
    capsys.readouterr()
    assert cli_main(["eval", "sweep", str(b)]) == 2
    assert "0.12.5" in capsys.readouterr().err
    # Gate flags set an in-process sidecar; a live one keeps its own.
    assert cli_main(["eval", "run", str(SET), "--socket", str(tmp_path / "none.sock"),
                     "--vault-min-cosine", "0.5", "--quiet"]) == 2


def test_compare_is_data_driven():
    a = {"set": {"sha256": "x"}, "status": "ok", "metrics": {"overall": {"stale_leak": 0}},
         "items": {"vault_hit@10": {"q1": True}}}
    b = {"set": {"sha256": "x"}, "status": "ok", "metrics": {"overall": {"stale_leak": 1}},
         "items": {"vault_hit@10": {"q1": True}}}
    tol = {"metrics": {"metrics.overall.stale_leak": {"max_rise": 0}}, "max_lost_gold": 0}
    assert compare(a, b, tol)["exit"] == 1
    tol["metrics"]["metrics.overall.stale_leak"]["max_rise"] = 1
    assert compare(a, b, tol)["exit"] == 0
    assert compare(a, {**b, "status": "gold_drift_excessive"}, tol)["exit"] == 3
    assert Receipt(a).status == "ok"


# ------------------------------------------------------------------ parity with the gate metrics

def _gate_metrics(db, evalset, monkeypatch):
    """The v0.11.0 gate's path: dense search -> evidence_bundle(k=10) -> path metrics (frozen),
    with the serving pool's per-note cap."""
    from quoth.db import connect_read_only
    from quoth.embed.backends import get_embedder
    from quoth.evidence import evidence_bundle
    from quoth.search import search

    rows = [{"id": r["id"], "origin": "authored", "expected_paths": vault_paths(r)} for r in evalset.rows]
    embedder = get_embedder("hash-ngram")
    conn = connect_read_only(db)
    try:
        from test_gate_parity import one_per_note
        bundles = [evidence_bundle(one_per_note(search(conn, r["question"], k=40, retriever="dense",
                                                       model="hash-ngram", embedder=embedder)), k=10)["hits"]
                   for r in evalset.rows]
    finally:
        conn.close()
    judged = []
    for row, bundle in zip(rows, bundles, strict=True):
        expected = set(row["expected_paths"])
        if not expected:
            continue
        paths = [h["path"] for h in bundle]
        ranks = [i for i, p in enumerate(paths, 1) if p in expected]
        judged.append((row["id"], int(any(p in expected for p in paths[:5])), int(bool(ranks)),
                       1 / ranks[0] if ranks else 0.0, len(expected & set(paths)) / len(expected)))
    n = len(judged)
    return {"n": n, "hit@5": sum(j[1] for j in judged) / n, "hit@10": sum(j[2] for j in judged) / n,
            "mrr@10": sum(j[3] for j in judged) / n, "recall@10": sum(j[4] for j in judged) / n,
            "items": {j[0]: bool(j[2]) for j in judged}}


def _assert_parity(old, receipt):
    vault = receipt.data["metrics"]["overall"]["stores"]["vault"]
    assert vault["n"] == old["n"]
    for new_key, old_key in (("hit@5", "hit@5"), ("hit@10", "hit@10"), ("mrr", "mrr@10"),
                             ("recall@10", "recall@10")):
        assert vault[new_key] == pytest.approx(old[old_key], abs=1e-4), new_key
    hits = receipt.data["items"]["vault_hit@10"]
    assert {i: v for i, v in old["items"].items() if i in hits} == hits


def test_parity_with_gate_metrics_on_public_fixture(built, run, monkeypatch):
    receipt, _ = run
    _assert_parity(_gate_metrics(Path(built["paths"]["db"]), load_set(SET), monkeypatch), receipt)


def test_parity_with_gate_metrics_on_legacy_golden(tmp_path, monkeypatch):
    vault = write_vault(tmp_path / "vault")
    db = build_snapshot(vault, tmp_path / "snap.db", "hash-ngram")
    build_ledger([], tmp_path / "ledger.db")
    convert("golden-v1", REPO / "eval" / "golden.jsonl", tmp_path / "golden.jsonl", privacy="public",
            fixture="tests/conftest.py", origin="synthetic")
    evalset = load_set(tmp_path / "golden.jsonl")
    target = InProcessTarget.build(db=db, ledger=tmp_path / "ledger.db", model="hash-ngram", retrieval="dense")
    try:
        receipt, _ = run_set(evalset, target)
    finally:
        target.close()
    old = _gate_metrics(db, evalset, monkeypatch)
    assert old["n"] == 10
    _assert_parity(old, receipt)
    assert receipt.data["metrics"]["overall"]["abstain"] == 4


# ------------------------------------------------------------------ prefetch log + online

def test_prefetch_log_prunes_old_files_and_survives_write_errors(tmp_path):
    from datetime import date
    from quoth.prefetch_log import PrefetchLog
    log_dir = tmp_path / "plog"
    log_dir.mkdir()
    (log_dir / "prefetch-2020-01-01.jsonl").write_text("{}\n")
    (log_dir / "notes.txt").write_text("kept")
    plog = PrefetchLog(log_dir, days=30)
    plog.write({"type": "prefetch", "ts": "t"})
    plog.flush()
    names = sorted(p.name for p in log_dir.iterdir())
    assert "prefetch-2020-01-01.jsonl" not in names and "notes.txt" in names
    assert plog.prune(today=date(2100, 1, 1)) == 1  # today's file is old by then
    plog.write({"bad": object()})  # not serialisable: logged once, never raised
    plog.flush()
    assert plog.errors == 1
    plog.close()


def test_online_counts_probable_misses_per_session(tmp_path):
    from datetime import datetime, timezone
    from quoth.evals.online import online
    day = datetime.now(tz=timezone.utc).date().isoformat()
    recs = [
        {"type": "prefetch", "ts": f"{day}T10:00:00", "session": "a", "query_len": 10,
         "stores": {"vault": [{"locator": "x", "score": 0.7, "injected": True}]}, "status": {"vault": "ok"},
         "ms": {"total": 5.0}},
        {"type": "tool", "ts": f"{day}T10:00:05", "session": "a", "tool": "episode_search", "k": 5, "results": []},
        {"type": "prefetch", "ts": f"{day}T10:01:00", "session": "a", "query_len": 4,
         "stores": {"vault": []}, "status": {"vault": "slow"}, "ms": {"total": 1500.0}},
        {"type": "prefetch", "ts": f"{day}T10:02:00", "session": "b", "query_len": 4,
         "stores": {"claims": [{"id": 1, "score": None, "injected": True}]}, "status": {}, "ms": {"total": 2.0}},
    ]
    (tmp_path / f"prefetch-{day}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs) + "garbage\n")
    out = online(tmp_path, days=7)
    assert out["turns"] == 3 and out["sessions"] == 2 and out["window"]["bad_lines"] == 1
    assert out["probable_miss_rate"] == round(1 / 3, 4)
    assert out["probable_miss_by_tool"]["episode_search"] == round(1 / 3, 4)
    assert out["empty_recall_rate"] == round(1 / 3, 4)
    assert out["injected_rate"]["vault"] == round(1 / 3, 4) and out["statuses"]["vault"] == {"ok": 1, "slow": 1}
    assert out["latency_ms"]["total"]["p95"] == 1500.0
    assert cli_main(["eval", "online", "--log", str(tmp_path), "--days", "7"]) == 0


def test_shipped_golden_fixture_is_synthetic():
    # eval/golden.jsonl is the SYNTHETIC golden-v1 fixture aligned with tests/conftest.py (the
    # real sets are private and stay on the host); converted, it is a valid eval set v1.
    from quoth.evals.convert import convert_in_memory, read_jsonl
    rows = read_jsonl(REPO / "eval" / "golden.jsonl")
    assert rows and all(r["id"].startswith("s") for r in rows)
    assert all(r["expected_paths"] == [] for r in rows if r["kind"] == "adversarial")
    assert {r["lang"] for r in rows} == {"en", "ru"}
    evalset = convert_in_memory("golden-v1", REPO / "eval" / "golden.jsonl")
    assert [to_golden_v1(r) for r in evalset.rows] == rows
    assert evalset.manifest["source"]["format"] == "golden-v1"


def test_cli_oracle_join_is_a_separate_private_file(built, tmp_path, capsys):
    fixture_dir = str(Path(built["paths"]["db"]).parent)
    rows = load_set(SET).rows
    oracle = tmp_path / "oracle.jsonl"
    oracle.write_text("".join(json.dumps({"id": r["id"], "oracle": [{"locator": "x.md:1-2", "text": f"src {r['id']}"}]})
                              + "\n" for r in rows[:2]) + json.dumps({"id": "not-in-set", "oracle": "x"}) + "\n")
    trace = tmp_path / "run.trace.jsonl"
    args = ["eval", "run", str(SET), "--fixture-dir", fixture_dir, "--trace-out", str(trace), "--quiet"]
    assert cli_main([*args, "--oracle", str(oracle)]) == 0
    err = capsys.readouterr().err
    joined = tmp_path / "run.trace.oracle.jsonl"
    assert "oracle join: 2/" in err and "1 oracle ids not in the run" in err
    assert oct(joined.stat().st_mode & 0o777) == "0o600"
    plain = [json.loads(x) for x in trace.read_text().splitlines()]
    lines = [json.loads(x) for x in joined.read_text().splitlines()]
    assert all("oracle" not in t for t in plain)  # the trace itself never holds oracle text
    assert [t["id"] for t in lines] == [t["id"] for t in plain]
    assert lines[0]["oracle"][0]["text"] == f"src {rows[0]['id']}" and lines[0]["oracle_found"]
    assert lines[-1]["oracle"] is None and lines[-1]["oracle_found"] is False
    assert cli_main(["eval", "run", str(SET), "--fixture-dir", fixture_dir, "--oracle", str(oracle),
                     "--quiet"]) == 2  # needs --trace-out


def test_cli_trace_out_and_both_retrieval_modes(built, tmp_path, capsys):
    """`--trace-out` writes the caller-test trace; the explicit bundle is scored in the receipt."""
    fixture_dir = str(Path(built["paths"]["db"]).parent)
    evalset = load_set(SET)
    receipts = {}
    for mode in ("hybrid", "dense"):
        out, trace = tmp_path / f"{mode}.json", tmp_path / f"{mode}.trace.jsonl"
        assert cli_main(["eval", "run", str(SET), "--fixture-dir", fixture_dir, "--out", str(out),
                         "--trace-out", str(trace), "--retrieval", mode, "--quiet"]) == 0
        err = capsys.readouterr().err
        assert "--trace-out" in err and "private" in err and oct(trace.stat().st_mode & 0o777) == "0o600"
        receipts[mode] = json.loads(out.read_text())
        lines = [json.loads(x) for x in trace.read_text().splitlines()]
        assert [r["id"] for r in lines] == [r["id"] for r in evalset.rows]
        first = lines[0]
        assert set(first) == {"id", "snapshot_id", "quoth_version", "settings", "query", "lang",
                              "prefetch_block", "explicit", "labels"}
        assert first["settings"]["retrieval"] == mode and first["snapshot_id"]
        assert first["query"] == evalset.rows[0]["question"]
        assert set(first["labels"]) == {"answerable", "kind"}
        assert any(r["explicit"] and set(r["explicit"][0]) == {"locator", "note_date", "project", "live", "text"}
                   for r in lines)
        assert any(r["prefetch_block"].startswith("<quoth-recall>") for r in lines)
    for mode, data in receipts.items():
        assert data["config"]["retrieval"] == mode
        assert data["explicit"]["calls"] > 0 and data["explicit"]["hit@10"] is not None
        assert "explicit" in json.dumps(data["latency_ms"])
