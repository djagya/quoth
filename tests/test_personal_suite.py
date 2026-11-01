"""Personal suite hook: score your own suite on a real snapshot (docs/EVAL.md#personal-suite).

Marked `personal`. Runs only when both are set, and skips otherwise:

- ``QUOTH_PERSONAL_SUITE``: a suite file (quoth-evalsuite/v1) listing your private sets;
- ``QUOTH_PERSONAL_DB``: the frozen snapshot (or serving pointer) to score it on.

Optional:

- ``QUOTH_PERSONAL_BASELINE``: a previous suite-run directory (``QUOTH_PERSONAL_OUT`` of an
  earlier run, or ``suite-runs/<receipt id>/candidate`` of a nightly ``SWITCH``); the run
  must compare to it with exit 0, by the nightly gate's rules;
- ``QUOTH_PERSONAL_OUT``: keep a copy of this run there, the baseline of a later one;
- ``QUOTH_PERSONAL_MODEL``: the embedding model (default ``bge-m3``).

Scoring is `run_suite_on_snapshot`, the function `refresh-nightly --suite` scores both
snapshots with, so personal and public sets run the same way. Outputs go to pytest's tmp
dir; what is printed is set ids, roles and aggregates only, never a question or a path.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil

import pytest

from quoth import nightly
from quoth.evals.suite import compare_suites, load_suite, run_suite_on_snapshot, without_roles

pytestmark = pytest.mark.personal


@pytest.fixture
def personal() -> dict:
    suite, db = os.environ.get("QUOTH_PERSONAL_SUITE"), os.environ.get("QUOTH_PERSONAL_DB")
    if not suite or not db:
        pytest.skip("set QUOTH_PERSONAL_SUITE and QUOTH_PERSONAL_DB to score a personal suite")
    return {"suite": Path(suite), "db": Path(db).resolve(),
            "baseline": os.environ.get("QUOTH_PERSONAL_BASELINE") or None,
            "out": os.environ.get("QUOTH_PERSONAL_OUT") or None,
            "model": os.environ.get("QUOTH_PERSONAL_MODEL") or "bge-m3"}


def test_personal_suite(personal, tmp_path):
    suite = without_roles(load_suite(personal["suite"]), nightly.SUITE_SKIPPED_ROLES)
    out = tmp_path / "run"
    summary = run_suite_on_snapshot(suite, personal["db"], out, model=personal["model"])
    view = {sid: {"role": s["role"], "status": s["status"], **s["headline"]}
            for sid, s in summary["sets"].items()}
    print(json.dumps({"suite": suite.name, "sets": view}, sort_keys=True))
    if personal["out"]:
        shutil.copytree(out, personal["out"], dirs_exist_ok=True)
    not_ok = sorted(sid for sid, s in summary["sets"].items() if s["status"] != "ok")
    assert not not_ok, f"sets not scored (gold drift?): {not_ok}"
    if personal["baseline"]:
        result = compare_suites(personal["baseline"], out, suite,
                                tolerances_for=lambda sid, tol: nightly.suite_tolerances(tol))
        report = {sid: {"role": r["role"], "result": r["result"],
                        "failed_checks": [c["metric"] for c in r["compare"]["checks"] if c["result"] == "fail"],
                        "lost_gold": r["compare"]["lost_gold"]}
                  for sid, r in result["sets"].items()}
        print(json.dumps({"compare": result["result"], "problems": result["problems"], "sets": report},
                         sort_keys=True))
        assert result["exit"] == 0, f"personal suite {result['result']} against the baseline"
