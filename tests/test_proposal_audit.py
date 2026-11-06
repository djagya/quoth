"""deploy/diagnostics/quoth-proposal-audit.py: aggregate counts from a real-schema ledger, read-only,
with no claim text, quotes, notes, locators or ids in its output."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from quoth.ledger import core

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "diagnostics" / "quoth-proposal-audit.py"
NOW = "2026-11-01T00:00:00.000Z"
MIRROR_ADD = "mirrored built-in memory add"
# Words that appear only in claim text, keys, notes and batch locators: none may reach the output.
PRIVATE = ("zebra", "Oscarville", "Papatown", "tango", "yankee", "zulu", "Victor", "Quebec", "Kilo",
           "home_city", "diet", "hermes:", "approved", "gamma", "delta", "Zarqon")


def _mirror(conn, text, at, action="add"):
    return core.propose(conn, text=text, kind="unclassified", origin="imported", proposer="builtin_memory",
                        reason=f"mirrored built-in memory {action}", now=at)["claim"]["id"]


def _assistant(conn, text, kind, at, **kw):
    return core.propose(conn, text=text, kind=kind, origin="assistant_proposal", proposer="assistant",
                        reason="seems durable", now=at, **kw)["claim"]["id"]


@pytest.fixture
def ledger_path(tmp_path):
    """A synthetic ledger through the real migrations and the ledger's own write paths."""
    path = tmp_path / "ledger.db"
    conn = core.open_ledger(path)
    # --- 60 days ago (outside the 30-day window)
    c1 = _mirror(conn, "Owner likes zebra tea alpha in the morning", "2026-09-01T09:00:00.000Z")
    c2 = _mirror(conn, "Task note: fixed the cron gamma bravo", "2026-09-01T09:01:00.000Z")
    c3 = _assistant(conn, "Owner prefers short answers delta", "preference", "2026-09-01T09:02:00.000Z")
    core.propose(conn, text="Owner is called Zarqon", kind="identity", origin="owner_statement",
                 proposer="assistant", evidence_quote="I am called Zarqon", evidence_locator="hermes:s0:1",
                 confidence=0.95, verifier=lambda loc, quote: True, now="2026-09-01T09:03:00.000Z")
    c14a = _assistant(conn, "Owner eats yankee", "preference", "2026-09-01T09:04:00.000Z", subject_key="diet")
    for cid, action in ((c1, "reject"), (c2, "stale"), (c3, "accept")):
        core.suggest(conn, cid, action=action, by="assistant", now="2026-09-01T10:00:00.000Z")
    applied = core.apply_suggestions(conn, [c1, c2, c3], note="approved via hermes:s1:101",
                                     now="2026-09-02T09:00:00.000Z", batch="hermes:s1:101")
    assert all("error" not in r for r in applied)
    core.reject(conn, c14a, now="2026-09-03T09:00:00.000Z")  # CLI: no batch
    _mirror(conn, "Victor whiskey xray", "2026-09-05T09:00:00.000Z")  # never decided: expires
    # --- inside the window
    c5 = _mirror(conn, "owner likes zebra tea alpha in the morning.", "2026-10-20T10:00:00.000Z")
    core.reject(conn, c5, note="approved via hermes:s2:5", now="2026-10-20T10:30:00.000Z",
                batch="hermes:s2:5")
    _mirror(conn, "Owner likes zebra tea alpha in the late morning", "2026-10-21T09:00:00.000Z", "replace")
    c7 = _mirror(conn, "Kilo lima mike note", "2026-10-21T10:00:00.000Z")
    _assistant(conn, "Owner lives in Oscarville", "identity", "2026-10-22T09:00:00.000Z", subject_key="home_city")
    c10 = _mirror(conn, "Quebec romeo sierra", "2026-10-22T10:00:00.000Z")
    _assistant(conn, "Owner lives in Papatown", "identity", "2026-10-23T09:00:00.000Z", subject_key="home_city")
    c11 = _assistant(conn, "Owner enjoys tango uniform", "taste", "2026-10-23T10:00:00.000Z")
    _assistant(conn, "Owner eats zulu food", "preference", "2026-10-24T09:00:00.000Z", subject_key="diet")
    assert core.propose(conn, text="Owner prefers short answers delta!", kind="preference",
                        origin="assistant_proposal", proposer="assistant",
                        now="2026-10-24T10:00:00.000Z")["decision"] == "noop_duplicate"
    core.suggest(conn, c7, action="accept", suggested_kind="environment", by="assistant",
                 now="2026-10-24T11:00:00.000Z")
    core.suggest(conn, c10, action="duplicate", duplicate_of=c3, by="assistant", now="2026-10-24T11:00:00.000Z")
    core.suggest(conn, c11, action="accept", by="assistant:cron", now="2026-10-24T11:00:00.000Z")
    applied = core.apply_suggestions(conn, [c7, c10], note="approved via hermes:s3:7",
                                     now="2026-10-25T09:00:00.000Z", batch="hermes:s3:7")
    assert [r.get("applied") for r in applied] == ["accept", "duplicate"]
    core.reject(conn, c11, note="approved via hermes:s3:9", now="2026-10-26T09:00:00.000Z",
                batch="hermes:s3:9")
    assert len(core.expire_pending(conn, 30, now=NOW)) == 1
    conn.close()
    return path


def _run(path: Path, *args: str) -> dict:
    # -I: isolated mode, so quoth is not importable and the script must stand on the stdlib alone.
    proc = subprocess.run([sys.executable, "-I", str(SCRIPT), str(path), "--now", NOW, *args],
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _keys(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _keys(v)
    elif isinstance(value, list):
        for v in value:
            yield from _keys(v)


def test_outcomes_and_rejection_share(ledger_path):
    out = _run(ledger_path)
    assert out["schema"] == "quoth-proposal-audit/1" and out["ledger"]["opened"] == "ro"
    assert out["ledger"]["migrations"] == [1, 2, 3, 4]
    assert out["ledger"]["proposals"] == 14 and out["ledger"]["edit_claims"] == 1
    full = out["all_time"]
    assert {k: full[k] for k in ("total", "accepted", "accepted_edited", "rejected", "duplicate_rejected",
                                 "superseded", "expired", "pending", "auto_accepted", "owner_decided")} == {
        "total": 14, "accepted": 2, "accepted_edited": 1, "rejected": 5, "duplicate_rejected": 1,
        "superseded": 1, "expired": 1, "pending": 3, "auto_accepted": 1, "owner_decided": 8}
    assert full["rejection_share"] == 0.75
    recent = out["last_30d"]
    assert (recent["total"], recent["rejected"], recent["duplicate_rejected"], recent["accepted_edited"],
            recent["pending"], recent["expired"]) == (8, 2, 1, 1, 3, 0)

    imported = full["by"]["origin"]["imported"]
    assert (imported["total"], imported["rejected"], imported["duplicate_rejected"],
            imported["rejection_share"]) == (7, 3, 1, 0.8)
    assert full["by"]["proposer"]["builtin_memory"]["total"] == 7
    assert full["by"]["mirror_action"]["memory:replace"]["pending"] == 1
    assert full["by"]["kind_at_proposal"]["unclassified"]["total"] == 7
    assert full["by"]["kind_at_decision"]["environment"]["accepted_edited"] == 1
    assert full["by"]["quote_verified"]["1"]["auto_accepted"] == 1
    assert full["by"]["suggestion"]["none"]["total"] == 8
    assert full["by"]["suggested_by"]["assistant:cron"]["rejected"] == 1
    assert full["origin_x_suggestion"]["imported"]["reject"]["rejected"] == 1
    assert full["by"]["text_length"]["<50"]["total"] == 14
    assert full["text_shape"]["says_user_or_owner"]["total"] == 10
    assert full["text_shape"]["none"]["total"] == 4


def test_text_shapes_and_length_cap(tmp_path):
    path = tmp_path / "ledger.db"
    conn = core.open_ledger(path)
    at = "2026-10-30T00:00:00.000Z"
    _mirror(conn, "Deployed via `make ship` to ~/srv/app/ on 2026-10-29, see https://x.invalid/a", at)
    _mirror(conn, "I fixed it. Then I checked. Then I left.\nDone", at)
    _mirror(conn, "y" * core.MAX_CLAIM_CHARS, at)
    conn.close()
    out = _run(path)["all_time"]
    shapes = {name: v["total"] for name, v in out["text_shape"].items()}
    assert shapes == {"url": 1, "path": 1, "date_or_time": 1, "code_or_command": 1, "multi_line": 1,
                      "first_person": 1, "many_sentences": 1, "none": 1}
    assert out["by"]["text_length"]["600 (cap)"]["total"] == 1


def test_decision_paths_timing_and_batches(ledger_path):
    full = _run(ledger_path)["all_time"]
    assert full["reject_path"] == {"applied_suggestion": 3, "direct": 2, "no_batch": 1}
    assert full["time_to_decision"]["<1h"]["rejected"] == 1
    assert sum(v["total"] for v in full["time_to_decision"].values()) == 8
    batches = full["decision_batch_size"]
    assert (batches["2-9"]["total"], batches["1"]["total"], batches["no_batch"]["total"]) == (5, 2, 1)


def test_presort_precision_and_recall(ledger_path):
    presort = _run(ledger_path)["all_time"]["presort"]
    assert (presort["owner_decided"], presort["with_suggestion"], presort["suggestion_coverage"]) == (8, 6, 0.75)
    assert presort["agreement"] == 0.8333
    accept = presort["per_action"]["accept"]
    assert (accept["decided"], accept["matched"], accept["precision"], accept["owner_edited"]) == (3, 2, 0.6667, 1)
    assert presort["per_action"]["duplicate"]["precision"] == 1.0
    assert presort["recall"]["keep"]["recall"] == 1.0 and presort["recall"]["drop"]["recall"] == 0.75


def test_reproposals_and_repeat_junk(ledger_path):
    out = _run(ledger_path)
    reps = out["all_time"]["reproposals"]
    assert reps["same_text_after_rejection"] == {"rejected": 1, "total": 1}
    assert reps["same_subject_key_after_rejection"] == {"pending": 1, "total": 1}
    assert reps["near_text_after_rejection"] == {"pending": 1, "total": 1}
    junk = reps["repeat_junk"]
    assert (junk["rejected"], junk["same_text_as_earlier_rejected"], junk["distinct_rejected_texts"]) == (6, 1, 5)
    assert junk["texts_by_copies"] == {"1": 4, "2": 1}
    # In the window the earlier rejection still counts (it is older than the window).
    assert out["last_30d"]["reproposals"]["repeat_junk"]["same_text_as_earlier_rejected"] == 1
    assert out["events"]["claims_re_proposed_as_noop_duplicate"] == 1
    assert out["events"]["all_time"]["expired_age"] == 1
    assert sum(sum(day.values()) for day in out["proposals_per_day_last_30d"].values()) == 8


def test_output_has_no_text_ids_or_locators_and_leaves_the_ledger_unchanged(ledger_path):
    before = hashlib.sha256(ledger_path.read_bytes()).hexdigest()
    raw = json.dumps(_run(ledger_path))
    for word in PRIVATE:
        assert word.lower() not in raw.lower(), word
    assert not {"id", "ids", "claim_id", "text", "quote", "note", "locator"} & set(_keys(json.loads(raw)))
    assert hashlib.sha256(ledger_path.read_bytes()).hexdigest() == before


def test_unknown_proposer_names_are_not_echoed(tmp_path):
    path = tmp_path / "ledger.db"
    conn = core.open_ledger(path)
    core.propose(conn, text="A fact", kind="pointer", origin="assistant_proposal",
                 proposer="Someone Private <x@example.org>", now="2026-10-30T00:00:00.000Z")
    conn.close()
    out = _run(path)
    assert out["all_time"]["by"]["proposer"] == {"other": out["all_time"]["by"]["proposer"]["other"]}
    assert "example" not in json.dumps(out)


def test_missing_ledger_fails_cleanly(tmp_path):
    proc = subprocess.run([sys.executable, "-I", str(SCRIPT), str(tmp_path / "none.db")],
                          capture_output=True, text=True, check=False)
    assert proc.returncode != 0 and "no ledger" in proc.stderr
