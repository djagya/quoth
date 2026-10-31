"""Paired significance tests of `eval compare` (`quoth.evals.stats`)."""
from __future__ import annotations

import pytest

from quoth.evals.compare import compare
from quoth.evals.stats import binom_two_sided, mcnemar, paired, permutation


def test_binomial_tail_is_exact():
    assert binom_two_sided(0, 5) == pytest.approx(2 / 32)
    assert binom_two_sided(1, 9) == pytest.approx(2 * 10 / 512)
    assert binom_two_sided(3, 6) == 1.0
    assert binom_two_sided(0, 0) == 1.0


def test_mcnemar_counts_only_rows_that_differ():
    a = {f"q{i}": i < 10 for i in range(20)}
    b = dict(a)
    for i in range(10, 18):  # 8 gained
        b[f"q{i}"] = True
    b["q0"] = False  # 1 lost
    out = mcnemar(a, b)
    assert (out["n"], out["gained"], out["lost"]) == (20, 8, 1)
    assert out["p"] == pytest.approx(round(2 * 10 / 512, 4))
    # Rows only one side scored, or scored as None, are not pairs.
    assert mcnemar({"x": True, "y": None}, {"x": False, "z": True})["n"] == 1


def test_permutation_is_exact_for_few_differences():
    a = {f"q{i}": 0.0 for i in range(10)}
    b = {**a, **{f"q{i}": 1.0 for i in range(5)}}
    out = permutation(a, b)
    assert out["method"] == "exact" and out["differing"] == 5
    assert out["delta"] == 0.5 and out["p"] == pytest.approx(2 / 32)
    assert permutation(a, a)["p"] == 1.0


def test_permutation_monte_carlo_is_seeded():
    a = {f"q{i}": 0.0 for i in range(40)}
    b = {f"q{i}": (0.5 if i % 4 else -0.25) for i in range(40)}
    first, second = permutation(a, b), permutation(a, b)
    assert first == second and first["method"].startswith("monte_carlo")
    assert 0 < first["p"] < 0.05


def _receipt(hits10, ranks):
    return {"set": {"sha256": "x"}, "status": "ok", "metrics": {"overall": {}},
            "items": {"vault_hit@10": hits10, "vault_rank": ranks,
                      "abstain_injected": {"u1": True, "u2": True}}}


def test_compare_reports_paired_tests_without_changing_the_exit():
    ids = [f"q{i}" for i in range(12)]
    a = _receipt({i: False for i in ids}, {i: None for i in ids})
    b = _receipt({i: True for i in ids}, {i: 1 for i in ids})
    b["items"]["abstain_injected"] = {"u1": False, "u2": False}
    out = compare(a, b, {"metrics": {}})
    assert out["exit"] == 0
    hit = out["paired"]["vault_hit@10"]
    assert hit["gained"] == 12 and hit["significant"] and hit["direction"] == "better"
    assert out["paired"]["vault_rr"]["delta"] == 1.0 and out["paired"]["vault_rr"]["direction"] == "better"
    # Fewer abstain rows injected is better.
    assert out["paired"]["abstain_injected"]["direction"] == "better"
    assert not out["paired"]["abstain_injected"]["significant"]
    # Swapped, the same tests read worse.
    assert compare(b, a, {"metrics": {}})["paired"]["vault_hit@10"]["direction"] == "worse"


def test_paired_skips_items_one_receipt_lacks():
    assert paired({"items": {"vault_hit@10": {"q": True}}}, {"items": {}}) == {}
