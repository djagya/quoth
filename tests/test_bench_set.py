"""The public retrieval benchmark: the persona vault (eval/bench) and its v2 set
(eval/sets/bench). Shape and coverage floors here; the hash-embedder run checks plumbing."""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re

import pytest

from quoth.evals import InProcessTarget, load_set, run_set
from quoth.evals.fixture import build_fixture
from quoth.evals.format import scored
from quoth.evals.suite import load_entry, load_suite

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "eval" / "bench"
VAULT = FIXTURE / "vault"
SET = REPO / "eval" / "sets" / "bench" / "set.jsonl"
CARD = REPO / "eval" / "sets" / "bench" / "CARD.md"
SPOTCHECK = REPO / "eval" / "sets" / "bench" / "SPOTCHECK.md"
REASONS = ("not_in_vault", "false_premise", "near_miss")


@pytest.fixture(scope="module")
def bench():
    return load_set(SET)


@pytest.fixture(scope="module")
def rows(bench):
    return [r for r in bench.rows if scored(r)]


def _paths(srcs):
    return [s["match"]["path"] for s in srcs if s["store"] == "vault"]


def _notes(row):
    return _paths(row["expect"]["sources"]) + _paths(row["expect"].get("negatives", []))


def test_manifest_is_public_and_points_at_the_fixture(bench):
    m = bench.manifest
    assert m["schema"] == "quoth-evalset/v2" and m["privacy"] == "public" and m["fixture"] == "eval/bench"
    assert CARD.is_file() and SPOTCHECK.is_file()
    assert not (FIXTURE / "set.jsonl").exists()  # the set lives under eval/sets/bench


def test_size_and_coverage_floors(rows):
    answerable = [r for r in rows if not r["expect"]["abstain"]]
    abstain = [r for r in rows if r["expect"]["abstain"]]
    assert len(answerable) >= 300 and len(abstain) >= 110
    reasons = Counter(r.get("abstain_reason") for r in abstain)
    assert all(reasons[k] >= 25 for k in REASONS) and set(reasons) <= set(REASONS)
    grouped = [r for r in answerable if r["expect"].get("groups")]
    assert len(grouped) >= 40 and all(2 <= len(r["expect"]["groups"]) <= 3 for r in grouped)
    assert all(r["hops"] == len(r["expect"]["groups"]) for r in grouped)
    assert sum(1 for r in answerable if r["expect"].get("negatives")) >= 60
    assert all(r["expect"].get("negatives") for r in abstain if r["abstain_reason"] == "near_miss")
    assert sum(1 for r in rows if r.get("paraphrase_of")) >= 30
    assert sum(1 for r in rows if r.get("context")) >= 8
    assert {r["kind"] for r in rows} >= {"fact", "person", "temporal", "decision", "knowledge_update",
                                         "episodic", "followup", "preference", "project_state", "unanswerable"}


def test_languages_are_balanced(rows):
    langs = Counter(r["lang"] for r in rows)
    assert set(langs) == {"en", "ru"}
    assert min(langs.values()) / sum(langs.values()) >= 0.4
    for kind in (True, False):
        sub = Counter(r["lang"] for r in rows if r["expect"]["abstain"] is kind)
        assert min(sub.values()) / sum(sub.values()) >= 0.3


def test_splits_are_by_cluster_and_never_share_a_note(rows):
    split_of_cluster, split_of_note = {}, {}
    for r in rows:
        assert r["split"] in ("dev", "test") and r["cluster"]
        assert split_of_cluster.setdefault(r["cluster"], r["split"]) == r["split"], r["cluster"]
        for path in _notes(r):
            assert split_of_note.setdefault(path, r["split"]) == r["split"], (r["id"], path)
    dev = sum(r["split"] == "dev" for r in rows) / len(rows)
    assert 0.3 <= dev <= 0.5
    assert len(split_of_cluster) >= 20


def test_every_path_exists_and_every_note_is_referenced(bench):
    vault = {p.relative_to(VAULT).as_posix() for p in VAULT.rglob("*.md")}
    assert 60 <= len(vault) <= 80
    used = Counter(p for r in bench.rows for p in _notes(r))
    assert set(used) <= vault
    assert vault <= set(used)  # hole audit: no note left out of the benchmark
    answered = Counter(p for r in bench.rows for p in _paths(r["expect"]["sources"]))
    assert min(answered[p] for p in vault) >= 2


def test_provenance_and_paraphrases(bench, rows):
    ids = {r["id"] for r in bench.rows}
    for r in bench.rows:
        prov = r["provenance"]
        assert prov["author"].startswith("llm:")
        if prov["review"] in ("accepted", "edited"):
            assert prov.get("reviewed_by")
        if r.get("paraphrase_of"):
            assert r["paraphrase_of"] in ids
    assert len(rows) / len(bench.rows) >= 0.9


def test_fixture_is_synthetic_text_only():
    """Public repo: no host paths, e-mail addresses or links in the vault or the set."""
    bad = re.compile(r"/Users/|/home/|[\w.+-]+@[\w-]+\.[\w.]+|https?://")
    for path in [*VAULT.rglob("*"), SET]:
        if path.is_file():
            assert not bad.search(path.read_text(encoding="utf-8")), path
    assert {p.name for p in FIXTURE.iterdir()} == {"vault"}  # vault only: no ledger seed, no messages


def test_bench_suite_loads():
    suite = load_suite(REPO / "eval" / "suites" / "bench.json")
    assert suite.fixture == "eval/bench"
    roles = {e.id: (e.role, e.split) for e in suite.entries}
    assert roles["bench-test"] == ("quality", "test") and roles["bench-dev"] == ("tune", "dev")
    sets = {e.id: load_entry(suite, e) for e in suite.entries}
    assert {r["split"] for r in sets["bench-test"].rows} == {"test"}
    assert {r["split"] for r in sets["bench-dev"].rows} == {"dev"}


# ------------------------------------------------------------------ hash-embedder plumbing run

@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return build_fixture(FIXTURE, tmp_path_factory.mktemp("bench-fixture"))


def test_bench_fixture_runs_end_to_end(built, bench):
    # hash-ngram is not semantic: this checks plumbing (every gold path resolves in the snapshot,
    # v2 metrics present), not quality. No gate: hash cosines sit under the bge-m3 floor.
    from quoth.relevance import Gate
    built_info = json.loads((Path(built["paths"]["db"]).parent / "fixture.json").read_text())
    assert built_info["built"]["ledger"] == {"accepted": 0, "pending": 0, "rejected": 0}
    target = InProcessTarget.build(**built["paths"], model=built["model"], gate=Gate())
    try:
        receipt, _ = run_set(bench.select("test"), target, gold_drift=True)
    finally:
        target.close()
    data = receipt.data
    assert data["status"] == "ok" and data["gold_drift"]["stale"] == 0 and data["gold_drift"]["renamed"] == 0
    assert data["scored"] == len(bench.select("test").rows) and data["set"]["split"] == "test"
    overall = data["metrics"]["overall"]
    assert overall["groups"]["n"] > 0 and "negative_leak_rate" in overall
    assert set(data["metrics"]["by_abstain_reason"]) == set(REASONS)
    assert set(data["metrics"]["by_lang"]) == {"en", "ru"}
    assert overall["stores"]["vault"]["hit@10"] > 0.3  # lexical overlap alone finds a share
