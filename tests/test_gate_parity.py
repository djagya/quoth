"""Gate parity: the nightly gate on `quoth.evals` decides exactly as the v0.11.0 evaluator did.

`legacy_evaluate_pair` is a frozen copy of the retired evaluator (golden-v1 rows, dense
`search` -> `evidence_bundle(k=10)` -> the private runner's scorer), kept here as the test
oracle. Both engines score the same snapshot pairs, built from the public fixture vault
with a deterministic 1024-dim hash embedder standing in for bge-m3, and every scenario's
gate decision (outcome, reasons, lost and excused ids, gold drift) must agree in both gate
modes. Scenarios cover each outcome: SWITCH, HOLD on lost gold, lost gold excused because
its note changed (content mode), renamed gold, and excessive gold drift (FAIL).
"""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

from quoth import nightly
from quoth.db import connect_read_only, open_db
from quoth.embed.backends import HashEmbedder
from quoth.embed.store import embed_chunks
from quoth.evals import load_set, write_set
from quoth.evals.convert import to_golden_v1
from quoth.evidence import evidence_bundle
from quoth.freshness import digest, model_contract, successor
from quoth.gold_drift import (ExcessiveDrift, canonical_hits, resolve, snapshot_identities,
                              snapshot_inventory)
from quoth.ingest import ingest_vault
from quoth.refresh import code_digests, inventory, sha, validate
from quoth.search import search

REPO = Path(__file__).resolve().parents[1]
PUBLIC = REPO / "eval" / "public"
EMB = HashEmbedder(SimpleNamespace(name="bge-m3", dim=1024))
RUNNER = '''def validate(rows):
    assert rows and len({r['id'] for r in rows}) == len(rows)
def metrics(h5, h10, expected):
    ranks = [i for i, h in enumerate(h10, 1) if h['path'] in expected]
    return {'hit@10': int(bool(ranks)), 'mrr@10': 1 / ranks[0] if ranks else 0}
'''


def legacy_evaluate_pair(current, candidate, golden_rows, private_rows, adapter):
    """Frozen v0.11.0 `nightly._evaluate_pair` scoring (inputs already loaded)."""
    rows = {"private": private_rows, "synthetic": golden_rows}
    original_paths = {name: {q["id"]: q["expected_paths"] for q in questions}
                      for name, questions in rows.items()}
    old_inventory = snapshot_identities(current)
    rows, aliases, drift = resolve(rows, old_inventory, snapshot_inventory(candidate))
    adapter.validate(rows["private"])
    if any(not r["expected_paths"] for r in rows["synthetic"] if r["kind"] != "adversarial"):
        raise ValueError("synthetic_gold_missing")
    outputs = []
    for db in (current, candidate):
        result = {}
        with closing(connect_read_only(db)) as conn:
            for dataset, questions in rows.items():
                judged = []
                for q in questions:
                    expected = set(q["expected_paths"])
                    if not expected or q.get("kind") == "adversarial":
                        continue
                    hits = search(conn, q["question"], k=10, retriever="dense",
                                  model="bge-m3", embedder=EMB)
                    row_aliases = {p: aliases[p] for p in original_paths[dataset][q["id"]] if p in aliases}
                    bundle = canonical_hits(evidence_bundle(hits, k=10)["hits"], row_aliases,
                                            old_inventory if db == current else None)
                    score = adapter.metrics(bundle[:5], bundle, expected)
                    recall = len(expected & {h["path"] for h in bundle}) / len(expected)
                    judged.append((q["id"], score["hit@10"], score["mrr@10"], recall,
                                   sorted(set(original_paths[dataset][q["id"]]))))
                result[dataset] = {"n": len(judged),
                    "hit@10": sum(r[1] for r in judged) / len(judged),
                    "mrr@10": sum(r[2] for r in judged) / len(judged),
                    "recall@10": sum(r[3] for r in judged) / len(judged),
                    "items": {r[0]: bool(r[1]) for r in judged},
                    "gold": {r[0]: r[4] for r in judged}}
        outputs.append(result)
    return outputs[0], outputs[1], drift


def frozen_snapshot(vault: Path, db: Path) -> Path:
    """An admitted serving snapshot of `vault` (current code and model contract)."""
    conn = open_db(db)
    ingest_vault(conn, vault)
    embed_chunks(conn, EMB)
    counts = validate(conn, inventory(vault))
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()
    db.with_suffix(".manifest.json").write_text(json.dumps({
        "db": str(db), "db_sha256": sha(db), "db_bytes": db.stat().st_size,
        "status": "frozen_current_vault_candidate", "vault_root": str(vault),
        "model": "BAAI/bge-m3", "integrity": "ok", "schema": "validate_evidence_db",
        "counts": counts, "code_digests": code_digests(),
        "model_manifest_sha256": model_contract(),
        "source_inventory_sha256": digest(inventory(vault))}))
    return db


def fillers(vault: Path, topic: str, n: int = 12) -> None:
    for i in range(n):
        (vault / "FILLER" / f"{topic} {i}.md").parent.mkdir(exist_ok=True)
        (vault / "FILLER" / f"{topic} {i}.md").write_text(
            f"# {topic}\n\n{topic}? {topic} {topic}, note {i} about {topic}.\n")


def switch(vault):
    (vault / "MISC").mkdir()
    (vault / "MISC" / "Bicycle bell.md").write_text("# Bicycle bell\n\nThe bell rings twice.\n")


def lost_gold(vault):
    fillers(vault, "Who is Orla Quenby")


def excused_gold(vault):
    (vault / "PROJECTS" / "Lantern.md").write_text("# Lantern\n\nArchived; see the wiki.\n")
    fillers(vault, "status of project Lantern")


def renamed_gold(vault):
    (vault / "HOME" / "Apartment.md").rename(vault / "HOME" / "Flat.md")


def drift_excessive(vault):
    (vault / "JOURNAL" / "2026-03-14.md").unlink()


SCENARIOS = {
    # name: (vault change, expected (content-mode outcome, code-mode outcome) or "drift")
    "switch": (switch, ("SWITCH", "SWITCH")),
    "lost_gold": (lost_gold, ("HOLD", "HOLD")),
    # Excused in content mode (the gold note changed), but one of ~13 items is past the
    # 0.03 content tolerance, so both modes hold: on metrics only in content mode.
    "excused_gold": (excused_gold, ("HOLD", "HOLD")),
    "renamed_gold": (renamed_gold, ("SWITCH", "SWITCH")),
    "drift_excessive": (drift_excessive, "drift"),
}


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    root = tmp_path_factory.mktemp("parity-base")
    shutil.copytree(PUBLIC / "vault", root / "vault")
    return frozen_snapshot(root / "vault", root / "current.db"), root / "vault"


def decide(a, b, mode, changes):
    reasons, lost, excused = nightly.gate(a, b, mode=mode, note_changes=changes)
    return {"outcome": "HOLD" if reasons else "SWITCH", "reasons": reasons, "lost": lost,
            "excused": excused}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_old_and_new_gate_decisions_agree_on_the_public_fixture(scenario, base, tmp_path, monkeypatch):
    current, base_vault = base
    vault = tmp_path / "vault"
    shutil.copytree(base_vault, vault)
    # The candidate is the current snapshot's successor; the current one keeps its own vault.
    work = tmp_path / "current.db"
    shutil.copy(current, work)
    manifest = json.loads(current.with_suffix(".manifest.json").read_text())
    manifest.update(db=str(work), vault_root=str(vault))
    work.with_suffix(".manifest.json").write_text(json.dumps(manifest))
    change, expected = SCENARIOS[scenario]
    change(vault)
    candidate = tmp_path / "candidate.db"
    successor(work, candidate, vault, embedder=EMB)

    # The golden-v1-expressible part of the public set: vault rows and unanswerables (the
    # legacy evaluator refused rows whose only gold is in claims or the episode lanes).
    rows = [r for r in load_set(PUBLIC / "set.jsonl").rows
            if r["expect"]["abstain"] or any(s["store"] == "vault" for s in r["expect"]["sources"])]
    gate_set = tmp_path / "gate.evalset.jsonl"
    write_set(rows, gate_set, privacy="public", fixture="eval/public")
    evalset = load_set(gate_set)
    gate_rows = [to_golden_v1(r) for r in evalset.rows]
    private_rows = [{**r, "id": "v2-" + r["id"]} for r in gate_rows if r["expected_paths"]]
    (tmp_path / "real-questions-v2.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in private_rows))
    runner = tmp_path / "runner.py"
    runner.write_text(RUNNER)
    adapter = SimpleNamespace()
    exec(RUNNER, adapter.__dict__)
    monkeypatch.setattr(nightly, "get_embedder", lambda *a, **kw: EMB)

    # Legacy kinds: unanswerables were "adversarial" in golden-v1.
    legacy_rows = [{**r, "kind": "adversarial" if r["kind"] == "unanswerable" else r["kind"]}
                   for r in gate_rows]
    if expected == "drift":
        with pytest.raises(ExcessiveDrift) as old_error:
            legacy_evaluate_pair(work, candidate, legacy_rows, private_rows, adapter)
        with pytest.raises(ExcessiveDrift) as new_error:
            nightly.evaluate_pair(work, candidate, gate_set, runner)
        assert new_error.value.report == old_error.value.report
        assert new_error.value.report["stale"] > 0
        return

    old_a, old_b, old_drift = legacy_evaluate_pair(work, candidate, legacy_rows, private_rows, adapter)
    new_a, new_b, evidence = nightly.evaluate_pair(work, candidate, gate_set, runner)
    assert evidence["gold_drift"] == old_drift
    assert evidence["gate_set"]["sha256"] == evalset.sha256
    for old, new in ((old_a, new_a), (old_b, new_b)):
        for dataset in ("private", "synthetic"):
            assert new[dataset]["items"] == old[dataset]["items"]
            assert new[dataset]["gold"] == old[dataset]["gold"]
            for metric in ("n", "hit@10", "mrr@10", "recall@10"):
                assert new[dataset][metric] == pytest.approx(old[dataset][metric]), (dataset, metric)
    changes = nightly.note_changes(work, candidate)
    outcomes = []
    for mode in ("content", "code"):
        old_decision = decide(old_a, old_b, mode, changes if mode == "content" else None)
        new_decision = decide(new_a, new_b, mode, changes if mode == "content" else None)
        assert new_decision == old_decision, mode
        outcomes.append(new_decision["outcome"])
    assert tuple(outcomes) == expected
    if scenario == "renamed_gold":
        assert old_drift["renamed"] > 0 and old_drift["stale"] == 0
    if scenario in ("lost_gold", "excused_gold"):
        assert any(decide(new_a, new_b, "code", None)["lost"].values())
        content = decide(new_a, new_b, "content", changes)
        lost_reasons = {r for r in content["reasons"] if r.endswith("_lost_all_gold")}
        if scenario == "excused_gold":
            assert any(content["excused"].values()) and not lost_reasons
        else:
            assert not any(content["excused"].values()) and lost_reasons
