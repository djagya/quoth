"""Eval set v2 (groups, negatives, splits, review state, abstain reasons) and suites."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from quoth.evals import metrics as M
from quoth.evals.format import (ABSTAIN_REASONS, EXPECT_KEYS, PROVENANCE_KEYS, REVIEWS, ROW_KEYS, SCHEMA,
                                SPLITS, EvalSetError, load_set, row_errors, write_set)
from quoth.evals.run import Receipt
from quoth.evals.suite import SuiteError, builtin_path, compare_suites, load_entry, load_suite, run_suite

REPO = Path(__file__).resolve().parents[1]


def _src(path):
    return {"store": "vault", "match": {"path": path}}


def _row(rid="r1", **kw):
    row = {"id": rid, "question": "which two notes say it", "lang": "en", "kind": "fact",
           "origin": "synthetic", "expect": {"abstain": False, "sources": [_src("a.md")]}}
    row.update(kw)
    return row


def test_v2_json_schema_matches_the_validator():
    schema = json.loads((REPO / "eval" / "schema" / "evalset-v2.schema.json").read_text())
    props = schema["properties"]
    assert set(props) == ROW_KEYS and set(props["expect"]["properties"]) == EXPECT_KEYS
    assert tuple(props["split"]["enum"]) == SPLITS
    assert tuple(props["abstain_reason"]["enum"]) == ABSTAIN_REASONS
    assert set(props["provenance"]["properties"]) == PROVENANCE_KEYS
    assert tuple(props["provenance"]["properties"]["review"]["enum"]) == REVIEWS
    assert SCHEMA in schema["$defs"]["manifest"]["properties"]["schema"]["enum"]


def test_groups_must_cover_exactly_the_sources():
    groups = [{"name": "a", "any_of": [_src("a.md")]}, {"name": "b", "any_of": [_src("b.md"), _src("c.md")]}]
    ok = _row(expect={"abstain": False, "sources": [_src("a.md"), _src("b.md"), _src("c.md")], "groups": groups})
    assert row_errors(ok) == []
    short = _row(expect={"abstain": False, "sources": [_src("a.md")], "groups": groups})
    assert any("exactly the groups' sources" in e for e in row_errors(short))
    one = _row(expect={"abstain": False, "sources": [_src("a.md")], "groups": groups[:1]})
    assert any("at least 2 groups" in e for e in row_errors(one))
    dup = _row(expect={"abstain": False, "sources": [_src("a.md"), _src("b.md"), _src("c.md")],
                       "groups": [groups[0], {**groups[1], "name": "a"}]})
    assert any("unique" in e for e in row_errors(dup))


def test_v2_row_fields_are_checked():
    assert row_errors(_row(split="dev", cluster="c-1", hops=2)) == []
    assert row_errors(_row(split="train"))
    assert row_errors(_row(hops=0)) and row_errors(_row(hops=True))
    assert row_errors(_row(abstain_reason="no_memory_needed"))  # answerable rows have no reason
    abstain = _row(kind="unanswerable", expect={"abstain": True, "sources": []}, abstain_reason="near_miss")
    assert row_errors(abstain) == []
    assert row_errors(_row(provenance={"author": "owner", "review": "accepted", "reviewed_at": "2026-10-31"})) == []
    assert row_errors(_row(provenance={"review": "accepted"}))
    assert row_errors(_row(provenance={"author": "owner", "review": "maybe"}))
    assert row_errors(_row(provenance={"author": "owner", "secret": 1}))
    assert row_errors(_row(paraphrase_of="r1"))  # not itself


def test_select_scores_reviewed_rows_of_one_split(tmp_path):
    rows = [_row("d1", split="dev"), _row("t1", split="test"), _row("both"),
            _row("draft", provenance={"author": "llm:x", "review": "draft"}),
            _row("para", paraphrase_of="d1")]
    write_set(rows, tmp_path / "s.jsonl", privacy="public", fixture="any")
    full = load_set(tmp_path / "s.jsonl")
    assert full.manifest["schema"] == SCHEMA and full.manifest["counts"]["review"] == {"accepted": 4, "draft": 1}
    dev = full.select("dev")
    assert [r["id"] for r in dev.rows] == ["d1", "both", "para"] and dev.skipped == 1 and dev.split == "dev"
    assert [r["id"] for r in full.select().rows] == ["d1", "t1", "both", "para"]
    with pytest.raises(EvalSetError):
        write_set([_row("x", paraphrase_of="nobody")], tmp_path / "bad.jsonl", privacy="public", fixture="any")


def test_v1_sets_still_load():
    evalset = load_set(REPO / "eval" / "public" / "set.jsonl")
    assert evalset.manifest["schema"] == "quoth-evalset/v1" and evalset.rows


def _item(path, injected=False):
    return {"path": path, "injected": injected}


def test_groups_and_negatives_metrics():
    groups = [{"name": "a", "any_of": [_src("a.md")]}, {"name": "b", "any_of": [_src("b.md")]}]
    row = _row(expect={"abstain": False, "sources": [_src("a.md"), _src("b.md")], "groups": groups,
                       "negatives": [_src("look-alike.md")]}, hops=2)
    both = M.observe(row, {"vault": [_item("a.md", True), _item("x.md"), _item("b.md", True)]})
    half = M.observe({**row, "id": "r2"}, {"vault": [_item("a.md", True), _item("look-alike.md", True)]})
    agg = M.aggregate([both, half])
    groups_block = agg["overall"]["groups"]
    assert groups_block["n"] == 2 and groups_block["all@10"] == 0.5 and groups_block["group_recall@10"] == 0.75
    assert groups_block["injected_all_rate"] == 0.5 and groups_block["all@5"] == 0.5
    assert agg["overall"]["negative_leak_rate"] == 0.5
    assert set(agg["by_hops"]) == {"2"}
    items = M.items([both, half])
    assert items["all@10"] == {"r1": True, "r2": False}
    assert agg["ci95"]["vault.hit@10"] == M.wilson(2, 2)


def test_wilson_interval():
    lo, hi = M.wilson(46, 49)
    assert 0.83 < lo < 0.85 and 0.97 < hi < 0.99
    assert M.wilson(0, 0) is None and M.wilson(0, 10)[0] == 0.0


def test_no_memory_set_is_portable_and_balanced():
    evalset = load_set(builtin_path("no-memory"))
    assert evalset.manifest["fixture"] == "any" and evalset.manifest["privacy"] == "public"
    assert len(evalset.rows) >= 100
    assert all(r["expect"]["abstain"] and r["abstain_reason"] == "no_memory_needed" for r in evalset.rows)
    assert evalset.manifest["counts"]["lang"]["en"] == evalset.manifest["counts"]["lang"]["ru"]
    assert (REPO / "eval" / "sets" / "no-memory" / "CARD.md").is_file()


def _suite(tmp_path, sets, fixture="eval/public"):
    path = tmp_path / "suite.json"
    path.write_text(json.dumps({"schema": "quoth-evalsuite/v1", "name": "t", "fixture": fixture, "sets": sets}))
    return path


def test_suite_files_are_checked(tmp_path):
    suite = load_suite(REPO / "eval" / "suites" / "public.json")
    assert [e.id for e in suite.entries] == ["fixture", "no-memory"] and suite.fixture == "eval/public"
    assert all(load_entry(suite, e).rows for e in suite.entries)
    with pytest.raises(SuiteError):
        load_suite(_suite(tmp_path, [{"id": "x", "builtin": "no-memory", "role": "judge"}]))
    with pytest.raises(SuiteError):
        load_suite(_suite(tmp_path, [{"id": "x", "builtin": "../etc", "role": "smoke"}]))
    with pytest.raises(SuiteError):
        load_suite(_suite(tmp_path, [{"id": "x", "role": "smoke"}]))
    # A set written for other stores is refused, an `any` set runs anywhere.
    other = load_suite(_suite(tmp_path, [{"id": "f", "path": str(REPO / "eval/public/set.jsonl"), "role": "smoke"},
                                         {"id": "n", "builtin": "no-memory", "role": "smoke"}], fixture="box"))
    with pytest.raises(SuiteError):
        load_entry(other, other.entries[0])
    assert load_entry(other, other.entries[1]).rows


def _fake_run(hit10):
    def run(evalset, target, **kw):
        ids = [r["id"] for r in evalset.rows]
        data = {"schema": "quoth-eval-run/v1", "set": {"sha256": evalset.sha256, "split": evalset.split},
                "status": "ok", "scored": len(ids),
                "metrics": {"overall": {"stores": {"vault": {"hit@10": hit10}}, "stale_leak": 0}, "ci95": {}},
                "items": {"vault_hit@10": {i: n < round(hit10 * len(ids)) for n, i in enumerate(ids)},
                          "injected_hit": {i: n < round(hit10 * len(ids)) for n, i in enumerate(ids)}}}
        return Receipt(data), None
    return run


def test_suite_run_and_compare_by_role(tmp_path):
    suite = load_suite(_suite(tmp_path, [{"id": "nm", "builtin": "no-memory", "role": "quality"},
                                         {"id": "guard", "builtin": "no-memory", "role": "regression"},
                                         {"id": "tune", "builtin": "no-memory", "role": "tune", "split": "dev"}]))
    tol = {"metrics": {"metrics.overall.stores.vault.hit@10": {"max_drop": 0.0}}, "max_lost_gold": 99}
    (tmp_path / "tol.json").write_text(json.dumps(tol))
    suite.tolerances = tmp_path / "tol.json"
    a = run_suite(suite, None, tmp_path / "a", run_set_fn=_fake_run(0.9))
    assert set(a["sets"]) == {"nm", "guard", "tune"} and (tmp_path / "a" / "nm.receipt.json").is_file()
    assert a["sets"]["nm"]["headline"]["vault.hit@10"] == 0.9
    run_suite(suite, None, tmp_path / "b", run_set_fn=_fake_run(0.88))
    out = compare_suites(tmp_path / "a", tmp_path / "b", suite)
    # 0.90 -> 0.88 over 120 rows: the regression guard fails, the quality set only on significance.
    assert out["sets"]["guard"]["exit"] == 1 and out["sets"]["nm"]["exit"] == 0 and "tune" not in out["sets"]
    assert out["exit"] == 1
    run_suite(suite, None, tmp_path / "c", run_set_fn=_fake_run(0.5))
    worse = compare_suites(tmp_path / "a", tmp_path / "c", suite)
    assert worse["sets"]["nm"]["exit"] == 1 and "vault_hit@10" in worse["sets"]["nm"]["compare"]["quality_failed_on"]
