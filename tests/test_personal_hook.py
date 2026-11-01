"""The personal pytest hook (tests/test_personal_suite.py) runs, skips and compares as documented.

Driven in a subprocess on the public fixture (hash-ngram model) standing in for a personal
snapshot and suite; nothing personal is read.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from quoth import nightly
from quoth.evals.fixture import build_fixture
from quoth.evals.suite import load_suite

REPO = Path(__file__).resolve().parents[1]
ENV_KEYS = ("QUOTH_PERSONAL_SUITE", "QUOTH_PERSONAL_DB", "QUOTH_PERSONAL_BASELINE",
            "QUOTH_PERSONAL_OUT", "QUOTH_PERSONAL_MODEL")


def hook(**env):
    base = {k: v for k, v in os.environ.items() if k not in ENV_KEYS}
    base["PYTHONPATH"] = str(REPO / "src")
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-s", "-p", "no:cacheprovider",
                           "-m", "personal", "tests/test_personal_suite.py"],
                          cwd=REPO, env={**base, **env}, capture_output=True, text=True, timeout=600)


def test_the_marker_is_registered_and_the_hook_skips_without_its_variables():
    result = hook()
    assert result.returncode == 0, result.stdout[-2000:]
    assert "1 skipped" in result.stdout
    assert "PytestUnknownMarkWarning" not in result.stdout + result.stderr


def test_the_hook_scores_a_suite_keeps_a_baseline_and_compares_against_it(tmp_path):
    built = build_fixture(REPO / "eval" / "public", tmp_path / "fixture")
    env = {"QUOTH_PERSONAL_SUITE": str(REPO / "eval" / "suites" / "public.json"),
           "QUOTH_PERSONAL_DB": built["paths"]["db"], "QUOTH_PERSONAL_MODEL": built["model"]}
    first = hook(**env, QUOTH_PERSONAL_OUT=str(tmp_path / "baseline"))
    assert first.returncode == 0, first.stdout[-3000:]
    assert "1 passed" in first.stdout
    line = next(json.loads(x) for x in first.stdout.splitlines() if x.startswith('{"sets"'))
    assert set(line["sets"]) == {"fixture", "no-memory"}
    assert all(s["status"] == "ok" and s["scored"] > 0 for s in line["sets"].values())
    assert json.loads((tmp_path / "baseline" / "suite.json").read_text())["suite"]["name"] == "public"
    second = hook(**env, QUOTH_PERSONAL_BASELINE=str(tmp_path / "baseline"))
    assert second.returncode == 0, second.stdout[-3000:]
    compared = next(json.loads(x) for x in second.stdout.splitlines() if x.startswith('{"compare"'))
    assert compared["compare"] == "pass" and compared["problems"] == []
    # Never a question of the set in what the hook prints.
    from quoth.evals import load_set
    for row in load_set(REPO / "eval" / "public" / "set.jsonl").rows:
        assert row["question"] not in first.stdout + second.stdout


def test_a_baseline_that_scored_better_fails_the_hook(tmp_path):
    built = build_fixture(REPO / "eval" / "public", tmp_path / "fixture")
    env = {"QUOTH_PERSONAL_SUITE": str(REPO / "eval" / "suites" / "public.json"),
           "QUOTH_PERSONAL_DB": built["paths"]["db"], "QUOTH_PERSONAL_MODEL": built["model"]}
    assert hook(**env, QUOTH_PERSONAL_OUT=str(tmp_path / "baseline")).returncode == 0
    receipt = tmp_path / "baseline" / "fixture.receipt.json"
    data = json.loads(receipt.read_text())
    vault = data["metrics"]["overall"]["stores"]["vault"]
    # Pretend the baseline ranked better than the run can (the guard allows a 0.02 MRR drop).
    assert vault["mrr"] is not None and vault["mrr"] < 1.0
    vault["mrr"] = min(1.0, vault["mrr"] + 0.1)
    receipt.write_text(json.dumps(data))
    result = hook(**env, QUOTH_PERSONAL_BASELINE=str(tmp_path / "baseline"))
    assert result.returncode == 1 and "1 failed" in result.stdout
    compared = next(json.loads(x) for x in result.stdout.splitlines() if x.startswith('{"compare"'))
    assert compared["sets"]["fixture"]["failed_checks"] == ["metrics.overall.stores.vault.mrr"]


def test_the_example_personal_suite_is_a_valid_suite_for_the_nightly():
    suite = load_suite(REPO / "eval" / "suites" / "personal.example.json")
    roles = {e.id: e.role for e in suite.entries}
    assert suite.fixture == nightly.SUITE_FIXTURE
    assert {"regression", "quality", "tune"} <= set(roles.values())
    assert roles["no-memory"] == "regression"
    assert any(e.id == "no-memory" and e.path.parts[-2] == "no-memory" for e in suite.entries)
