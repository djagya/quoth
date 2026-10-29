"""Core-memory ledger: schema guarantees, quote verification, reconcile, review, core compile."""
import json
import os
import sqlite3
import unicodedata

import pytest

from quoth.ledger import core
from quoth.ledger.authored import StateDbVerifier, authored_text
from quoth.ledger.compile import HEADER, compile_core, publish_core

T0 = "2026-10-01T09:00:00.000Z"


@pytest.fixture
def ledger(tmp_path):
    conn = core.open_ledger(tmp_path / "ledger.db")
    yield conn
    conn.close()


@pytest.fixture
def state_db(tmp_path):
    path = tmp_path / "state.db"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT,
            parent_session_id TEXT, profile_name TEXT, user_id TEXT);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL,
            active INTEGER NOT NULL, timestamp REAL NOT NULL, content TEXT, display_kind TEXT,
            platform_message_id TEXT, compacted INTEGER NOT NULL DEFAULT 0);
    """)
    db.executemany("INSERT INTO sessions(id, source) VALUES (?, ?)",
                   [("tg1", "telegram"), ("cron1", "cron")])
    db.executemany(
        "INSERT INTO messages(id, session_id, role, active, timestamp, content, compacted)"
        " VALUES (?,?,?,?,?,?,?)", [
            (1, "tg1", "user", 1, 1.0, "[Ari] I moved to Quendorf last spring.", 0),
            (2, "tg1", "assistant", 1, 2.0, "Noted: you moved to Quendorf.", 0),
            (3, "cron1", "user", 1, 3.0, "I moved to Quendorf last spring.", 0),
            (4, "tg1", "user", 1, 4.0, "[SYSTEM: I moved to Quendorf last spring.]", 0),
            (5, "tg1", "user", 0, 5.0, "I moved to Quendorf last spring.", 1),
            (6, "tg1", "user", 0, 6.0, "inactive and not compacted text here", 0),
            (7, "tg1", "user", 1, 7.0,
             '[Replying to: "The assistant said something long"]\n\nPlease call me Platypus from now on.', 0),
            (8, "tg1", "user", 1, 8.0, "Café " + unicodedata.normalize("NFD", "é") + "tude", 0),
        ])
    db.commit()
    db.close()
    return path


def _owner(conn, verifier, text="Ari lives in Quendorf.", kind="identity", confidence=0.95, **kw):
    return core.propose(conn, text=text, kind=kind, origin="owner_statement", proposer="foreground",
                        evidence_quote="moved to Quendorf", evidence_locator="hermes:tg1:1",
                        confidence=confidence, verifier=verifier, **kw)


# ------------------------------------------------------------------ schema

def test_reopen_is_idempotent_and_schema_guards_hold(tmp_path):
    path = tmp_path / "ledger.db"
    conn = core.open_ledger(path)
    claim = core.propose(conn, text="Uses a standing desk.", kind="environment",
                         origin="assistant_proposal", proposer="foreground")["claim"]
    conn.close()
    conn = core.open_ledger(path)
    assert conn.execute("SELECT count(*) FROM ledger_migrations").fetchone()[0] == len(core._migrations())
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        conn.execute("DELETE FROM claims WHERE id = ?", (claim["id"],))
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE claims SET text = 'changed' WHERE id = ?", (claim["id"],))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM ledger_events")
    conn.close()


def test_ledger_is_separate_from_snapshot_schema(tmp_path):
    # The evidence snapshot migration list is untouched by the ledger.
    from quoth.db import list_migrations
    assert [v for v, _, _ in list_migrations()] == [1]
    conn = core.open_ledger(tmp_path / "l.db")
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'chunks'").fetchone()
    conn.close()


# ------------------------------------------------------------------ authored quotes

def test_verifier_accepts_only_owner_authored_telegram_text(state_db):
    verify = StateDbVerifier(state_db)
    assert verify("hermes:tg1:1", "moved to Quendorf last spring")
    assert verify("hermes:tg1:5", "moved to Quendorf")              # compacted original counts
    assert not verify("hermes:tg1:2", "moved to Quendorf")          # assistant
    assert not verify("hermes:cron1:3", "moved to Quendorf")        # non-Telegram source
    assert not verify("hermes:tg1:4", "moved to Quendorf")          # runtime-generated
    assert not verify("hermes:tg1:6", "inactive and not compacted")
    assert not verify("hermes:tg1:1", "moved to Berlin")            # not present
    assert not verify("hermes:tg1:1", "moved")                      # too short
    assert not verify("tg1:1", "moved to Quendorf last spring")     # malformed locator
    assert not verify("hermes:tg1:7", "assistant said something")   # reply context is not Ari's
    assert verify("hermes:tg1:7", "call me Platypus from now on")
    assert verify("hermes:tg1:8", "Café " + "é" + "tude")           # NFC == NFD
    assert authored_text(state_db, "tg1", 1) == "I moved to Quendorf last spring."


def test_owner_statement_requires_verified_quote(ledger, state_db):
    with pytest.raises(core.LedgerRejected, match="quote_not_verified"):
        core.propose(ledger, text="x is y", kind="identity", origin="owner_statement",
                     proposer="fg", evidence_quote="moved to Berlin", evidence_locator="hermes:tg1:1",
                     verifier=StateDbVerifier(state_db))
    with pytest.raises(core.LedgerRejected, match="quote_not_verified"):
        core.propose(ledger, text="x is y", kind="identity", origin="owner_statement",
                     proposer="fg", evidence_quote="moved to Quendorf", evidence_locator="hermes:tg1:1")
    with pytest.raises(core.LedgerRejected, match="quote_required"):
        core.propose(ledger, text="x is y", kind="identity", origin="owner_statement", proposer="fg")
    with pytest.raises(core.LedgerRejected, match="locator_required"):
        core.propose(ledger, text="x is y", kind="identity", origin="owner_note", proposer="fg")
    assert ledger.execute("SELECT count(*) FROM claims").fetchone()[0] == 0


# ------------------------------------------------------------------ type gate

def test_auto_accept_only_verified_owner_facts(ledger, state_db):
    verify = StateDbVerifier(state_db)
    fact = _owner(ledger, verify)
    assert fact["auto_accepted"] and fact["claim"]["review_status"] == "accepted"
    assert fact["claim"]["authority"] == "owner"
    pref = _owner(ledger, verify, text="Ari prefers living in Quendorf.", kind="preference")
    assert not pref["auto_accepted"] and pref["claim"]["review_status"] == "pending"
    low = _owner(ledger, verify, text="Ari may live in Quendorf.", confidence=0.6)
    assert low["claim"]["review_status"] == "pending"
    inferred = core.propose(ledger, text="Ari likes small towns.", kind="identity",
                            origin="assistant_proposal", proposer="background_review", confidence=1.0)
    assert inferred["claim"]["review_status"] == "pending"
    assert inferred["claim"]["authority"] == "assistant"


def test_undo_auto_accept_restores_superseded(ledger, state_db):
    verify = StateDbVerifier(state_db)
    old = core.propose(ledger, text="Ari lives in Berlin.", kind="identity", origin="assistant_proposal",
                       proposer="fg", subject_key="owner.city")["claim"]
    core.accept(ledger, old["id"])
    new = _owner(ledger, verify, subject_key="owner.city")
    assert new["decision"] == "supersede_proposed" and new["auto_accepted"]
    assert core.get(ledger, old["id"])["valid_to"] is not None
    undone = core.undo_auto_accept(ledger, new["claim"]["id"])
    assert undone["review_status"] == "pending"
    restored = core.get(ledger, old["id"])
    assert restored["current"] and restored["review_status"] == "accepted"
    with pytest.raises(core.LedgerRejected, match="not_auto_accepted"):
        core.undo_auto_accept(ledger, old["id"])


# ------------------------------------------------------------------ reconcile

def test_duplicate_is_noop_and_counted(ledger):
    first = core.propose(ledger, text="Works from the Quendorf office.", kind="environment",
                         origin="assistant_proposal", proposer="bg")
    again = core.propose(ledger, text="works from the  Quendorf office", kind="environment",
                         origin="assistant_proposal", proposer="bg")
    assert again["decision"] == "noop_duplicate" and again["claim"]["id"] == first["claim"]["id"]
    assert core.health(ledger)["last_7d"]["re_proposed"] == 1


def test_pending_latest_wins_and_closest_reported(ledger):
    a = core.propose(ledger, text="Platypus drinks green tea.", kind="preference",
                     origin="assistant_proposal", proposer="bg", subject_key="drink", now=T0)
    b = core.propose(ledger, text="Platypus drinks black coffee.", kind="preference",
                     origin="assistant_proposal", proposer="bg", subject_key="drink")
    assert b["decision"] == "replace_pending"
    old = core.get(ledger, a["claim"]["id"])
    assert old["review_status"] == "expired" and old["superseded_by"] == b["claim"]["id"]
    c = core.propose(ledger, text="Platypus drinks black coffee daily at work.", kind="preference",
                     origin="assistant_proposal", proposer="bg")
    assert [x["id"] for x in c["closest"]] == [b["claim"]["id"]]
    assert [q["id"] for q in core.review_queue(ledger)] == [b["claim"]["id"], c["claim"]["id"]]


def test_accepting_supersession_retires_old_value(ledger):
    old = core.propose(ledger, text="The build host is anvil.", kind="environment",
                       origin="assistant_proposal", proposer="bg", subject_key="host")["claim"]
    core.accept(ledger, old["id"])
    new = core.propose(ledger, text="The build host is forge.", kind="environment",
                       origin="assistant_proposal", proposer="bg", subject_key="host")
    assert new["decision"] == "supersede_proposed" and new["claim"]["supersedes"] == old["id"]
    # Until reviewed, recall still serves the old accepted value, never the pending one.
    assert [h["id"] for h in core.recall(ledger, "build host")] == [old["id"]]
    core.accept(ledger, new["claim"]["id"])
    assert [h["text"] for h in core.recall(ledger, "build host")] == ["The build host is forge."]
    retired = core.get(ledger, old["id"])
    assert retired["valid_to"] and retired["superseded_by"] == new["claim"]["id"]


# ------------------------------------------------------------------ review actions

def test_reject_edit_forget_expire_and_health(ledger):
    p = core.propose(ledger, text="Ari hates mornings.", kind="preference",
                     origin="assistant_proposal", proposer="bg", now=T0)["claim"]
    q = core.propose(ledger, text="Ari reads before sleep.", kind="preference",
                     origin="assistant_proposal", proposer="bg", now=T0)["claim"]
    r = core.propose(ledger, text="Ari runs on Sundays.", kind="episode",
                     origin="assistant_proposal", proposer="bg")["claim"]
    assert core.reject(ledger, p["id"], note="wrong")["review_status"] == "rejected"
    with pytest.raises(core.LedgerRejected, match="not_pending"):
        core.accept(ledger, p["id"])
    edited = core.edit(ledger, r["id"], text="Ari runs on Sunday mornings.")
    assert edited["review_status"] == "accepted" and edited["supersedes"] == r["id"]
    assert core.get(ledger, r["id"])["review_status"] == "expired"
    assert core.forget(ledger, edited["id"], "no longer true")["valid_to"] is not None
    assert core.recall(ledger, "runs Sunday") == []
    health = core.health(ledger, max_pending_age_days=14, now="2026-10-27T09:00:00.000Z")
    assert health["status"] == "red" and health["reasons"] == ["pending_queue_old"]
    assert core.expire_pending(ledger, 14, now="2026-10-27T09:00:00.000Z") == [q["id"]]
    assert core.health(ledger, now="2026-10-27T09:00:00.000Z")["status"] == "green"


def test_recall_marks_use(ledger):
    c = core.propose(ledger, text="Platypus is allergic to peanuts.", kind="identity",
                     origin="assistant_proposal", proposer="bg")["claim"]
    core.accept(ledger, c["id"])
    hits = core.recall(ledger, "peanuts allergy", mark_used=True)
    assert hits[0]["authority"] == "assistant"
    assert core.get(ledger, c["id"])["use_count"] == 1


def test_seed_latest_wins_and_requires_kind_on_review(ledger):
    report = core.seed(ledger, [{"text": "User likes jazz."}, {"text": "user likes jazz"},
                                {"text": "User lives in Quendorf.", "kind": "identity"}])
    assert report == {"input_unique": 2, "added": 2, "skipped_current_duplicates": 0}
    queue = core.review_queue(ledger)
    assert {q["origin"] for q in queue} == {"imported"}
    jazz = next(q for q in queue if "jazz" in q["text"])
    assert jazz["text"] == "user likes jazz" and jazz["kind"] == "unclassified"
    with pytest.raises(core.LedgerRejected, match="kind_required"):
        core.accept(ledger, jazz["id"])
    assert core.edit(ledger, jazz["id"], kind="taste")["kind"] == "taste"
    assert core.seed(ledger, [{"text": "User lives in Quendorf."}])["skipped_current_duplicates"] == 1


# ------------------------------------------------------------------ core compile

def _accepted(conn, text, kind, origin="assistant_proposal"):
    claim = core.propose(conn, text=text, kind=kind, origin=origin, proposer="t",
                         evidence_locator="ME/note.md" if origin == "owner_note" else None)["claim"]
    return core.accept(conn, claim["id"])


def test_compile_core_budget_order_and_labels(ledger):
    a = _accepted(ledger, "Name: Platypus.", "identity", "owner_note")
    b = _accepted(ledger, "Prefers short answers.", "preference")
    _accepted(ledger, "Ran 5 km on Sunday.", "episode")
    filler = [_accepted(ledger, f"Project {i} detail " + "x" * 40, "project_state") for i in range(20)]
    out = compile_core(ledger, budget_chars=600, headroom=0.2)
    assert out["chars"] <= out["limit"] == 480
    assert out["text"].startswith(HEADER)
    assert out["text"].index("## Identity") < out["text"].index("## Preferences")
    assert "- Name: Platypus.\n" in out["text"] and "Prefers short answers. [inferred]" in out["text"]
    assert "Ran 5 km" not in out["text"]
    assert out["claim_ids"][:2] == [a["id"], b["id"]] and out["dropped_ids"]
    assert set(out["dropped_ids"]) <= {c["id"] for c in filler}


def test_publish_core_is_immutable_and_atomic(ledger, tmp_path):
    _accepted(ledger, "Name: Platypus.", "identity")
    out = tmp_path / "core"
    first = publish_core(ledger, out, budget_chars=1000)
    again = publish_core(ledger, out, budget_chars=1000)
    assert first["sha256"] == again["sha256"]
    target = out / os.readlink(out / "core.md")
    assert target.name == first["file"] and oct(target.stat().st_mode & 0o777) == "0o444"
    assert json.loads((out / "core.json").read_text())["claim_ids"] == first["claim_ids"]
    _accepted(ledger, "Lives in Quendorf.", "identity")
    second = publish_core(ledger, out, budget_chars=1000)
    assert second["sha256"] != first["sha256"] and (out / first["file"]).exists()
    assert ledger.execute("SELECT count(*) FROM core_versions").fetchone()[0] == 2


def test_cli_round_trip(tmp_path, capsys):
    from quoth.ledger.cli import main
    db = str(tmp_path / "l.db")
    assert main(["--ledger", db, "propose", "--text", "Uses Linux at home.",
                 "--kind", "environment", "--origin", "assistant_proposal"]) == 0
    claim = json.loads(capsys.readouterr().out)["claim"]
    assert main(["--ledger", db, "accept", str(claim["id"])]) == 0
    capsys.readouterr()
    assert main(["--ledger", db, "compile-core"]) == 0
    assert "Uses Linux at home. [inferred]" in capsys.readouterr().out
    assert main(["--ledger", db, "accept", str(claim["id"])]) == 3  # refused
    assert json.loads(capsys.readouterr().err) == {"error": "not_pending"}


def test_cli_expire_dry_run_changes_nothing_and_defaults_to_quoth_home(tmp_path, monkeypatch, capsys):
    from quoth.ledger.cli import main
    monkeypatch.setenv("QUOTH_HOME", str(tmp_path / "home"))
    conn = core.open_ledger(tmp_path / "home" / "ledger.db")
    old = core.propose(conn, text="Old pending thing.", kind="taste", origin="assistant_proposal",
                       proposer="assistant", now=T0)["claim"]["id"]
    conn.close()
    assert main(["expire", "--days", "1", "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out) == {"would_expire": [old]}
    conn = core.open_ledger(tmp_path / "home" / "ledger.db")
    assert core.get(conn, old)["review_status"] == "pending"
    conn.close()
    assert main(["expire", "--days", "1"]) == 0
    assert json.loads(capsys.readouterr().out) == {"expired": [old]}


# ------------------------------------------------------------------ 0004: owner/assistant values

def _old_ledger(path):
    """A ledger at migration 0003 holding the stored values written before 0004."""
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("CREATE TABLE ledger_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL,"
                 " applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))")
    for version, name, sql in core._migrations()[:3]:
        for statement in core._statements(sql):
            conn.execute(statement)
        conn.execute("INSERT INTO ledger_migrations(version, name) VALUES (?, ?)", (version, name))
    conn.execute("PRAGMA foreign_keys = ON")
    claims = [
        (1, "Lives in Quendorf.", "identity", "danil_statement", "foreground", "accepted", None, 2),
        (2, "Lives in Brindle now.", "identity", "danil_statement", "danil", "accepted", 1, None),
        (3, "Prefers tea.", "preference", "sera_proposal", "sera", "pending", None, None),
        (4, "Keeps a kayak.", "environment", "sera_proposal", "sera:cron", "pending", None, None),
        (5, "Old memory line.", "unclassified", "builtin_mirror", "builtin_memory", "pending", None, None),
        (6, "Desk is oak.", "environment", "danil_vault", "review_edit", "rejected", None, None),
    ]
    conn.executemany("INSERT INTO claims(id, text, kind, origin, proposer, review_status, supersedes,"
                     " superseded_by, evidence_locator, recorded_at) VALUES (?,?,?,?,?,?,?,?, 'ME/n.md', ?)",
                     [(*c[:7], None, T0) for c in claims])
    conn.executemany("UPDATE claims SET superseded_by = ? WHERE id = ?", [(c[7], c[0]) for c in claims if c[7]])
    events = [
        (1, "proposed", "danil", json.dumps({"origin": "danil_statement", "kind": "identity"})),
        (3, "proposed", "sera", json.dumps({"origin": "sera_proposal", "decision": "add"})),
        (4, "re_proposed", "sera:cron", json.dumps({"origin": "sera_proposal"})),
        (5, "proposed", "seed", json.dumps({"origin": "builtin_mirror"})),
        (2, "accepted", "danil", json.dumps({"batch": "hermes:tg1:9"})),
        (3, "suggested", "sera", json.dumps({"suggestion": "accept"})),
        (6, "rejected", "auto", None),
        (6, "expired_age", "expiry", "not json"),
    ]
    conn.executemany("INSERT INTO ledger_events(claim_id, action, actor, detail, at) VALUES (?,?,?,?,?)",
                     [(*e, T0) for e in events])
    conn.executemany("INSERT INTO claim_suggestions(claim_id, action, duplicate_of, suggested_by, suggested_at)"
                     " VALUES (?,?,?,?,?)", [(3, "accept", None, "sera", T0), (4, "duplicate", 3, "sera:review", T0),
                                             (5, "keep", None, "operator", T0)])
    conn.close()


def test_0004_migrates_stored_identity_values_in_place(tmp_path):
    path = tmp_path / "ledger.db"
    _old_ledger(path)
    conn = core.open_ledger(path)
    assert [r[0] for r in conn.execute("SELECT version FROM ledger_migrations ORDER BY version")] == [1, 2, 3, 4]
    rows = conn.execute("SELECT id, origin, proposer, supersedes, superseded_by FROM claims ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [
        (1, "owner_statement", "foreground", None, 2), (2, "owner_statement", "owner", 1, None),
        (3, "assistant_proposal", "assistant", None, None), (4, "assistant_proposal", "assistant:cron", None, None),
        (5, "imported", "builtin_memory", None, None), (6, "owner_note", "review_edit", None, None)]
    assert {c["id"]: c["authority"] for c in map(_by_id(conn), range(1, 7))} == {
        1: "owner", 2: "owner", 3: "assistant", 4: "assistant", 5: "imported", 6: "owner"}
    events = conn.execute("SELECT claim_id, actor, detail FROM ledger_events ORDER BY id").fetchall()
    assert [r[1] for r in events] == ["owner", "assistant", "assistant:cron", "seed", "owner", "assistant",
                                      "auto", "expiry"]
    origins = [json.loads(r[2]).get("origin") if r[2] and r[2].startswith("{") else r[2] for r in events]
    assert origins == ["owner_statement", "assistant_proposal", "assistant_proposal", "imported", None, None,
                       None, "not json"]
    assert json.loads(events[0][2])["kind"] == "identity" and json.loads(events[4][2]) == {"batch": "hermes:tg1:9"}
    assert [tuple(r) for r in conn.execute(
        "SELECT claim_id, suggested_by, duplicate_of FROM claim_suggestions ORDER BY id")] == [
        (3, "assistant", None), (4, "assistant:review", 3), (5, "operator", None)]
    # Nothing lost, every guard back, references intact, new values accepted, old ones refused.
    assert conn.execute("SELECT count(*) FROM claims").fetchone()[0] == 6
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('index', 'trigger')")}
    assert {"claims_status", "claims_kind", "claims_subject", "claims_no_delete", "claims_content_immutable",
            "ledger_events_no_update", "ledger_events_no_delete"} <= names
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        conn.execute("DELETE FROM claims WHERE id = 3")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE claims SET origin = 'imported' WHERE id = 3")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE ledger_events SET actor = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute("INSERT INTO claims(text, kind, origin, proposer) VALUES ('x', 'taste', 'sera_proposal', 'a')")
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute("INSERT INTO ledger_events(claim_id, action, actor) VALUES (999, 'x', 'owner')")
    new = core.propose(conn, text="Rides an e-bike.", kind="environment", origin="assistant_proposal",
                       proposer="assistant")["claim"]
    assert new["id"] == 7 and core.accept(conn, 3)["review_status"] == "accepted"
    assert core.claim_events(conn, 3)[-1]["actor"] == "owner"
    conn.close()
    conn = core.open_ledger(path)  # reopening applies nothing again
    assert conn.execute("SELECT count(*) FROM ledger_migrations").fetchone()[0] == 4
    conn.close()


def _by_id(conn):
    return lambda claim_id: core.get(conn, claim_id)
