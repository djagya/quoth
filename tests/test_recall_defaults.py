"""The recall defaults have one home (`quoth.recall_defaults`): the docs quote the same
numbers, and every module and CLI that uses one reads it from there."""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from quoth import recall_defaults as D

DOCS = Path(__file__).resolve().parents[1] / "docs"


def _g(value) -> str:
    return f"{value:g}"


def _flag_table(text: str) -> dict[str, str]:
    """`| `--flag ARG` | default | ... |` rows of a Markdown flag table: {--flag: default}."""
    out = {}
    for m in re.finditer(r"^\| `(--[a-z-]+)[^`]*` \| ([^|]+) \|", text, re.M):
        out[m.group(1)] = m.group(2).strip()
    return out


HERMES_FLAGS = {
    "--evidence-budget": _g(D.EVIDENCE_BUDGET),
    "--claim-sync-interval": _g(D.CLAIM_SYNC_INTERVAL),
    "--owner-min-score": _g(D.OWNER_MIN_SCORE),
    "--assistant-min-score": _g(D.ASSISTANT_MIN_SCORE),
    "--episodes-interval": _g(D.EPISODES_INTERVAL),
    "--episodes-tick-seconds": _g(D.EPISODES_TICK_SECONDS),
    "--prefetch-log-days": _g(D.PREFETCH_LOG_DAYS),
    "--vault-min-cosine": _g(D.GATE["vault_min_cosine"]),
    "--vault-max-gap": _g(D.GATE["vault_max_gap"]),
    "--rerank-budget": _g(D.RERANK_BUDGET),
    "--rerank-max-tokens": _g(D.RERANK_MAX_TOKENS),
}


def test_hermes_flag_table_matches_recall_defaults():
    table = _flag_table((DOCS / "HERMES.md").read_text(encoding="utf-8"))
    for flag, value in HERMES_FLAGS.items():
        assert table.get(flag) == value, flag
    # Gate rules the defaults leave off are documented as off.
    off = [k for k, v in D.GATE.items() if v is None]
    text = (DOCS / "HERMES.md").read_text(encoding="utf-8")
    for name in off:
        flag = "--" + name.replace("_", "-")
        row = next(line for line in text.splitlines() if line.startswith("| ") and f"`{flag}`" in line)
        assert "| off |" in row, flag


def _phrases():
    """(doc, regex with one group, expected) for every number the recall prose quotes."""
    cap = f"{D.QUERY_MAX_CHARS:,}"
    return [
        ("HERMES.md", r"capped at ([\d,]+) characters", cap),
        ("HERMES.md", r"the (\d+) best vault hits", str(D.VAULT_POOL)),
        ("HERMES.md", r"up to (\d+)\s+accepted, current claims", str(D.CLAIM_POOL)),
        ("HERMES.md", r"then up to (\d+) by words", str(D.CLAIM_POOL)),
        ("HERMES.md", r"cosine ≥ ([\d.]+)", _g(D.CLAIM_INDEX_MIN_SIMILARITY)),
        ("HERMES.md", r"a vault hit's (\d+)-character excerpt", str(D.VAULT_SNIPPET)),
        ("HERMES.md", r"up to (\d+) claims and", str(D.PREFETCH_CLAIMS)),
        ("HERMES.md", r"the top (\d+) vault hits", str(D.PREFETCH_EVIDENCE)),
        ("HERMES.md", r"vault cosine floor of ([\d.]+)", _g(D.GATE["vault_min_cosine"])),
        ("HERMES.md", r"a gap of at most\s+([\d.]+)", _g(D.GATE["vault_max_gap"])),
        ("HERMES.md", r"`--evidence-budget` \(([\d.]+) s\)", _g(D.EVIDENCE_BUDGET)),
        ("HERMES.md", r"Up to (\d+) excerpts", str(D.PREFETCH_EPISODES)),
        ("HERMES.md", r"excerpts \((\d+) characters\)", str(D.EPISODE_SNIPPET)),
        ("HERMES.md", r"floor \(([\d.]+)\), from what is left", _g(D.OWNER_MIN_SCORE)),
        ("HERMES.md", r"\| `k_evidence` \| (\d+) \|", str(D.PREFETCH_EVIDENCE)),
        ("HERMES.md", r"\| `k_claims` \| (\d+) \|", str(D.PREFETCH_CLAIMS)),
        ("EVAL.md", r"injects the top (\d+) vault hits", str(D.PREFETCH_EVIDENCE)),
        ("EVAL.md", r"and up to (\d+) claims whatever", str(D.PREFETCH_CLAIMS)),
        ("EVAL.md", r"\(the (\d+) best vault hits", str(D.VAULT_POOL)),
        ("EVAL.md", r"up to (\d+)\s+claims by meaning", str(D.CLAIM_POOL)),
        ("EVAL.md", r"a vault floor of ([\d.]+)", _g(D.GATE["vault_min_cosine"])),
        ("EVAL.md", r"and a gap of ([\d.]+);", _g(D.GATE["vault_max_gap"])),
        ("EVAL.md", r"`--vault-min-cosine X` \(default ([\d.]+)\)", _g(D.GATE["vault_min_cosine"])),
        ("EVAL.md", r"`--vault-max-gap X` \(default ([\d.]+)\)", _g(D.GATE["vault_max_gap"])),
        ("EVAL.md", r"serving defaults \(`--vault-min-cosine ([\d.]+)", _g(D.GATE["vault_min_cosine"])),
        ("EVAL.md", r"\n\s*--vault-max-gap ([\d.]+)`\)", _g(D.GATE["vault_max_gap"])),
        ("EVAL.md", r"\(`--evidence-budget ([\d.]+)`", _g(D.EVIDENCE_BUDGET)),
        ("EVAL.md", r"`--rerank-budget ([\d.]+)`\)", _g(D.RERANK_BUDGET)),
        ("EVAL.md", r"`--rerank-budget` seconds \(default ([\d.]+)\)", _g(D.RERANK_BUDGET)),
        ("EVAL.md", r"`--rerank-max-tokens` \(default (\d+)\)", str(D.RERANK_MAX_TOKENS)),
        ("EVAL.md", r"excerpt \((\d+)\s+characters\)", str(D.VAULT_SNIPPET)),
        ("EVAL.md", r"\[--prefetch-log-days (\d+)\]", str(D.PREFETCH_LOG_DAYS)),
        ("EVAL.md", r"serve the best (\d+) by score", str(D.PREFETCH_EVIDENCE)),
    ]


@pytest.mark.parametrize("doc,pattern,expected", _phrases())
def test_docs_quote_recall_defaults(doc, pattern, expected):
    text = (DOCS / doc).read_text(encoding="utf-8")
    found = re.findall(pattern, text)
    assert found, f"{doc}: {pattern!r} not found"
    assert set(found) == {expected}, f"{doc}: {pattern!r} quotes {found}, recall_defaults has {expected}"


def test_flags_default_to_recall_defaults(monkeypatch):
    """The sidecar's, `eval run`'s and `eval sweep`'s flag defaults are the recall defaults."""
    from quoth import relevance, rerank, sidecar
    from quoth.evals import cli as eval_cli
    seen = {}
    monkeypatch.setattr(sidecar, "Quoth", lambda **kw: seen.update(kw) or (_ for _ in ()).throw(SystemExit(0)))
    with pytest.raises(SystemExit):
        sidecar.main(["--ledger", ":memory:", "--state-db", ""])
    assert seen["evidence_budget"] == D.EVIDENCE_BUDGET and seen["owner_min_score"] == D.OWNER_MIN_SCORE
    assert seen["episode_min_score"] == D.ASSISTANT_MIN_SCORE and seen["gate"] == relevance.Gate(**D.GATE)
    p = argparse.ArgumentParser()
    rerank.add_arguments(p)
    a = p.parse_args([])
    assert (a.rerank_budget, a.rerank_max_tokens) == (D.RERANK_BUDGET, D.RERANK_MAX_TOKENS)
    captured = {}
    monkeypatch.setattr(eval_cli, "_cmd_sweep", lambda a: captured.update(vars(a)) or 0)
    assert eval_cli.main(["sweep", "r.json"]) == 0
    assert (captured["rerank_budget"], captured["evidence_budget"]) == (D.RERANK_BUDGET, D.EVIDENCE_BUDGET)


def test_modules_read_their_numbers_from_recall_defaults():
    from quoth import claim_index, episodes, prefetch_log, relevance, rerank, sidecar
    from quoth.evals import sweep
    assert (sidecar.PREFETCH_CLAIMS, sidecar.PREFETCH_EVIDENCE, sidecar.PREFETCH_SNIPPET) == (
        D.PREFETCH_CLAIMS, D.PREFETCH_EVIDENCE, D.VAULT_SNIPPET)
    assert (sidecar.EVIDENCE_BUDGET, sidecar.EPISODES_INTERVAL, sidecar.QUERY_MAX_CHARS) == (
        D.EVIDENCE_BUDGET, D.EPISODES_INTERVAL, D.QUERY_MAX_CHARS)
    assert (sidecar.PREFETCH_EPISODES, sidecar.EPISODE_SNIPPET, sidecar.MAX_CANDIDATES) == (
        D.PREFETCH_EPISODES, D.EPISODE_SNIPPET, D.MAX_CANDIDATES)
    assert (sweep.PREFETCH_CLAIMS, sweep.PREFETCH_EVIDENCE) == (D.PREFETCH_CLAIMS, D.PREFETCH_EVIDENCE)
    assert (sweep.DEFAULT_RERANK_BUDGET, sweep.DEFAULT_EVIDENCE_BUDGET) == (D.RERANK_BUDGET, D.EVIDENCE_BUDGET)
    assert (rerank.DEFAULT_BUDGET, rerank.MAX_TOKENS) == (D.RERANK_BUDGET, D.RERANK_MAX_TOKENS)
    assert relevance.MAX_WIDEN == D.MAX_WIDEN and claim_index.MIN_SIMILARITY == D.CLAIM_INDEX_MIN_SIMILARITY
    assert episodes.EMBED_TICK_SECONDS == D.EPISODES_TICK_SECONDS
    assert prefetch_log.DEFAULT_DAYS == D.PREFETCH_LOG_DAYS
    assert set(D.GATE) == {f.name for f in __import__("dataclasses").fields(relevance.Gate)}


def test_retrieval_docstring_names_the_default():
    """The module docstring of `quoth.retrieval` names the serving default, not the opt-in."""
    import quoth.retrieval as R
    doc = R.__doc__ or ""
    assert f"**{D.RETRIEVAL}** (default, `recall_defaults.RETRIEVAL`)" in doc
    other = {"dense": "hybrid", "hybrid": "dense"}[D.RETRIEVAL]
    assert f"**{other}** (default" not in doc
