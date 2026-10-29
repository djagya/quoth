"""Guided review (0.13): migration 0003, topics and `covered`, digest, bucket apply, batch undo,
turn-bound approvals, the presort assist, the readable /quoth queue and the plugin schema.

Synthetic claims only."""
from __future__ import annotations

import sqlite3
import sys
import time

import numpy as np
import pytest

from quoth.claim_index import ClaimIndex
from quoth.episodes import TICK_LOCK
from quoth.ledger import core
from quoth.ledger.authored import is_assent, parse_review_reply, previous_authored, turn_approval
from test_sidecar import REPO, _call, _load_plugin, provider, served, snapshot_db  # noqa: F401 (fixtures)

T0 = "2026-10-01T09:00:00.000Z"


@pytest.fixture
def state_db(tmp_path):
    """Hermes state.db with one chat; `say` appends the next row."""
    path = tmp_path / "state.db"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT,
            parent_session_id TEXT, profile_name TEXT, user_id TEXT);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL,
            active INTEGER NOT NULL, timestamp REAL NOT NULL, content TEXT, display_kind TEXT,
            platform_message_id TEXT, compacted INTEGER NOT NULL DEFAULT 0, tool_calls TEXT);
        INSERT INTO sessions(id, source) VALUES ('tg1', 'telegram');
        INSERT INTO messages(id, session_id, role, active, timestamp, content)
            VALUES (1, 'tg1', 'user', 1, 1.0, '[Ari] review memory please');
    """)
    db.commit()
    db.close()
    return path


def say(state_db, text, role="user", tool_calls=None, display_kind=None):
    db = sqlite3.connect(state_db)
    n = db.execute("SELECT max(id) FROM messages").fetchone()[0] + 1
    db.execute("INSERT INTO messages(id, session_id, role, active, timestamp, content, tool_calls, display_kind)"
               " VALUES (?,?,?,?,?,?,?,?)", (n, "tg1", role, 1, float(n), text, tool_calls, display_kind))
    db.commit()
    db.close()
    return f"hermes:tg1:{n}"


def assistant_works(state_db):
    """The turn's own tool-call rows: they never end the turn."""
    say(state_db, "", role="assistant", tool_calls='[{"id": "c1"}]')
    say(state_db, '{"ok": true}', role="tool")


def assistant_replies(state_db, text="Done."):
    say(state_db, text, role="assistant")


@pytest.fixture
def ledger(tmp_path):
    conn = core.open_ledger(tmp_path / "ledger.db")
    yield conn
    conn.close()


def _seed(conn, *texts):
    core.seed(conn, [{"text": t} for t in texts], now=T0)
    return [cid for cid, _ in core.pending_current(conn)][-len(texts):]


def _suggest_all(provider, ids, suggestion, **kw):
    for cid in ids:
        _call(provider, "review_claims", action="suggest", id=cid, suggestion=suggestion, **kw)


# ------------------------------------------------------------------ ledger migration

def test_migration_0003_keeps_suggestions_and_allows_covered(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("CREATE TABLE ledger_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL,"
                 " applied_at TEXT NOT NULL DEFAULT 'x')")
    for version, name, sql in core._migrations()[:2]:  # a ledger as 0.12 left it
        for statement in core._statements(sql):
            conn.execute(statement)
        conn.execute("INSERT INTO ledger_migrations(version, name) VALUES (?, ?)", (version, name))
    conn.executemany("INSERT INTO claims(id, text, kind, origin, proposer) VALUES (?,?,?,?,?)",
                     [(1, "Fact one.", "unclassified", "builtin_mirror", "seed"),  # pre-0004 values
                      (2, "Fact two.", "unclassified", "builtin_mirror", "seed")])
    conn.executemany("INSERT INTO claim_suggestions(id, claim_id, action, duplicate_of, suggested_by,"
                     " suggested_at) VALUES (?,?,?,?,?,?)",
                     [(1, 1, "stale", None, "sera", T0), (2, 1, "reject", None, "sera", T0),
                      (3, 2, "duplicate", 1, "sera", T0)])
    conn.execute("UPDATE claim_suggestions SET replaced_by = 2 WHERE id = 1")
    conn.close()

    conn = core.open_ledger(path)
    assert [r[0] for r in conn.execute("SELECT version FROM ledger_migrations ORDER BY 1")] == [1, 2, 3, 4]
    rows = [tuple(r) for r in conn.execute("SELECT id, claim_id, action, duplicate_of, replaced_by, topic"
                                           " FROM claim_suggestions ORDER BY id")]
    assert rows == [(1, 1, "stale", None, 2, None), (2, 1, "reject", None, None, None),
                    (3, 2, "duplicate", 1, None, None)]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert core.current_suggestion(conn, 1)["action"] == "reject"
    # Re-sorting an already presorted claim: the old tip is replaced, never deleted.
    tip = core.suggest(conn, 1, action="covered", by="assistant", evidence_locator="NOTES/a.md:1-2",
                       topic="Home")
    assert tip["action"] == "covered" and tip["evidence_locator"] == "NOTES/a.md:1-2"
    assert conn.execute("SELECT replaced_by FROM claim_suggestions WHERE id = 2").fetchone()[0] == tip["id"]
    assert conn.execute("SELECT count(*) FROM claim_suggestions").fetchone()[0] == 4
    with pytest.raises(sqlite3.IntegrityError):  # the schema itself holds the topic bound
        conn.execute("UPDATE claim_suggestions SET topic = ? WHERE id = 1", ("x" * 41,))
    with pytest.raises(sqlite3.IntegrityError):  # and `covered` always names its note
        conn.execute("INSERT INTO claim_suggestions(claim_id, action, suggested_by, suggested_at)"
                     " VALUES (2, 'covered', 'assistant', ?)", (T0,))
    conn.close()
    core.open_ledger(path).close()  # idempotent


def test_suggest_topic_covered_and_resuggest(ledger):
    a, b = _seed(ledger, "Ari drinks green tea.", "Ari likes long walks.")
    with pytest.raises(core.LedgerRejected, match="locator_required"):
        core.suggest(ledger, a, action="covered", by="assistant")
    with pytest.raises(core.LedgerRejected, match="topic_length"):
        core.suggest(ledger, a, action="stale", by="assistant", topic="x" * 41)
    core.suggest(ledger, a, action="accept", by="assistant", suggested_kind="preference", topic="  Food &  health ")
    tip = core.suggest(ledger, a, action="covered", by="assistant", evidence_locator="FOOD/tea.md:3-4")
    assert tip["topic"] == "Food & health"  # kept from the earlier suggestion
    assert core.presort_summary(ledger) == {"covered": 1, "none": 1}
    out = core.apply_suggestions(ledger, [a, b], batch="hermes:tg1:9")
    assert out[0] == {"id": a, "applied": "covered", "result_id": a, "review_status": "rejected",
                      "text": "Ari drinks green tea."}
    assert out[1] == {"id": b, "error": "no_suggestion"}
    assert core.get(ledger, a)["review_note"] == "already in notes: FOOD/tea.md:3-4"


# ------------------------------------------------------------------ digest

def test_digest_plans_by_topic_and_lists_one_group(ledger):
    tea, tea2, walk, old, junk, odd, new = _seed(
        ledger, "Ari drinks green tea.", "Ari likes green tea a lot.", "Ari walks daily.",
        "Ari used to live in Brindle.", "Asked about the weather once.", "Uncertain thing.", "Unsorted one.")
    core.suggest(ledger, tea, action="accept", by="assistant", suggested_kind="preference", topic="Food & health",
                 rewrite="Ari prefers green tea.")
    core.suggest(ledger, tea2, action="duplicate", by="assistant", duplicate_of=tea, topic="Food & health")
    core.suggest(ledger, walk, action="accept", by="assistant", suggested_kind="preference", topic="Food & health")
    core.suggest(ledger, old, action="stale", by="assistant", topic="Home")
    core.suggest(ledger, junk, action="reject", by="assistant", topic="Home")
    core.suggest(ledger, odd, action="keep", by="assistant", topic="Home", note="ask the owner")
    d = core.digest(ledger)
    assert d["pending"] == 7
    assert d["by_suggestion"] == {"accept": 2, "duplicate": 1, "keep": 1, "none": 1, "reject": 1, "stale": 1}
    assert [t["topic"] for t in d["topics"]] == ["Food & health", "Home", "Other"]
    assert d["topics"][1] == {"topic": "Home", "total": 3, "to_read": 1,
                              "by_suggestion": {"stale": 1, "reject": 1, "keep": 1}}
    assert d["plan"] == {"cleanup": 3, "to_read": 4, "unsorted": 1, "round_size": 10, "rounds": 1}
    group = d["group"]
    assert group["topic"] == "Food & health" and group["total"] == 3
    assert [(i["n"], i["id"], i["suggestion"]) for i in group["items"]] == [
        (1, tea, "accept"), (2, walk, "accept"), (3, tea2, "duplicate")]
    assert group["items"][0]["text"] == "Ari prefers green tea." and group["items"][0]["kind"] == "preference"
    assert group["items"][2]["duplicate_of_text"] == "Ari prefers green tea."  # what stays
    home = core.digest(ledger, topic="home", limit=2)["group"]
    assert [i["id"] for i in home["items"]] == [odd, old] and home["total"] == 3
    assert [i["id"] for i in core.digest(ledger, bucket="none")["group"]["items"]] == [new]
    with pytest.raises(core.LedgerRejected, match="unknown_topic"):
        core.digest(ledger, topic="Nope")
    # Once the first topic has nothing left to read, the next one leads.
    core.apply_suggestions(ledger, [tea, walk])
    assert core.digest(ledger)["group"]["topic"] == "Home"


# ------------------------------------------------------------------ duplicates never drop every copy

def test_duplicate_applies_only_when_its_target_stays(ledger):
    a, b, g, h, d, s = _seed(ledger, "Copy A.", "Copy B.", "Copy G.", "Copy H.", "Copy D.", "Survivor S.")
    core.suggest(ledger, a, action="duplicate", by="assistant", duplicate_of=b)
    core.suggest(ledger, b, action="duplicate", by="assistant", duplicate_of=a)  # mutual: nothing stays
    core.suggest(ledger, h, action="reject", by="assistant")
    core.suggest(ledger, g, action="duplicate", by="assistant", duplicate_of=h)  # its target is dropped
    core.suggest(ledger, s, action="accept", by="assistant", suggested_kind="taste", rewrite="Survivor, clean.")
    core.suggest(ledger, d, action="duplicate", by="assistant", duplicate_of=s)
    items = {i["id"]: i for i in core.digest(ledger, bucket="duplicate")["group"]["items"]}
    assert items[a]["duplicate_target_kept"] is False and items[a]["duplicate_of_text"] is None
    assert items[g]["duplicate_target_kept"] is False and items[g]["duplicate_of_text"] is None
    assert items[d]["duplicate_target_kept"] is True and items[d]["duplicate_of_text"] == "Survivor, clean."
    out = {r["id"]: r for r in core.apply_suggestions(ledger, [h, a, b, g, s, d], batch="hermes:tg1:2")}
    for cid in (a, b, g):
        assert out[cid] == {"id": cid, "error": "duplicate_target_not_kept"}
        assert core.get(ledger, cid)["review_status"] == "pending"
    # S was accepted through an edit (a new claim superseding it): its copy still folds into it.
    assert out[s]["review_status"] == "accepted" and out[d]["review_status"] == "rejected"
    assert core.kept_target(ledger, s)["id"] == out[s]["result_id"]


def test_dropping_an_original_brings_its_duplicates_back(ledger):
    t, d1, d2 = _seed(ledger, "Original T.", "Copy one of T.", "Copy two of T.")
    core.suggest(ledger, t, action="accept", by="assistant", suggested_kind="taste")
    for d in (d1, d2):
        core.suggest(ledger, d, action="duplicate", by="assistant", duplicate_of=t)
    # Bulk clean-up in one reply drops the copies while T still waits with an accept suggestion.
    core.apply_suggestions(ledger, [d1, d2], batch="hermes:tg1:2")
    assert core.claim_events(ledger, d1, detail=True)[-1]["detail"] == {"batch": "hermes:tg1:2", "duplicate_of": t}
    # A later round drops T itself: its copies come back for review in the same transaction.
    out = core.reject(ledger, t, batch="hermes:tg1:4")
    assert out["review_status"] == "rejected" and out["restored_duplicates"] == [d1, d2]
    for d in (d1, d2):
        assert core.get(ledger, d)["review_status"] == "pending"
        assert core.claim_events(ledger, d, detail=True)[-1] == {
            "at": core.claim_events(ledger, d)[-1]["at"], "action": "restored", "actor": "owner",
            "detail": {"batch": "hermes:tg1:4", "target_dropped": t}}
    # Their suggestion still points at the dropped original, so they are never applied blind again.
    assert [r.get("error") for r in core.apply_suggestions(ledger, [d1])] == ["duplicate_target_not_kept"]
    # Undoing the earlier clean-up finds them changed; undoing T's drop brings T back.
    assert {e["id"] for e in core.undo_batch(ledger, "hermes:tg1:2")["errors"]} == {d1, d2}
    core.undo_batch(ledger, "hermes:tg1:4")
    assert core.get(ledger, t)["review_status"] == "pending"


def test_apply_then_drop_in_one_reply_keeps_the_copy(provider, served, state_db):
    app, _ = served
    with app.lock:
        t, d, t2, d2 = _seed(app.conn, "Original T.", "Copy of T.", "Original U.", "Copy of U.")
    _call(provider, "review_claims", action="suggest", id=t, suggestion="accept", kind="taste", topic="Home")
    _call(provider, "review_claims", action="suggest", id=d, suggestion="duplicate", duplicate_of=t, topic="Home")
    reply = "ok, drop 1"
    say(state_db, reply)
    # The assistant applies first and rejects second (the skill says the other way round): still nothing lost.
    applied = _call(provider, "review_claims", action="apply", ids=[d], approval_quote=reply)
    assert applied["results"][0]["review_status"] == "rejected"
    dropped = _call(provider, "review_claims", action="reject", ids=[t], approval_quote=reply)
    assert dropped["results"][0]["restored_duplicates"] == [d]
    with app.lock:
        assert core.get(app.conn, d)["review_status"] == "pending"
    # The same through apply: the original's suggestion was changed to reject.
    _call(provider, "review_claims", action="suggest", id=t2, suggestion="accept", kind="taste")
    _call(provider, "review_claims", action="suggest", id=d2, suggestion="duplicate", duplicate_of=t2)
    assistant_replies(state_db)
    say(state_db, "ok")
    _call(provider, "review_claims", action="apply", ids=[d2], approval_quote="ok")
    _call(provider, "review_claims", action="suggest", id=t2, suggestion="reject")
    out = _call(provider, "review_claims", action="apply", ids=[t2], approval_quote="ok")
    assert out["results"][0]["restored_duplicates"] == [d2]


def test_assist_does_not_yield_to_its_own_turn(provider, served):
    app, _ = served
    with app.lock:
        _seed(app.conn, "Some fact.")
    app._last_query_at, app._last_query_session = time.monotonic(), "tg1"  # this chat's own prefetch
    out = _call(provider, "review_claims", action="assist")
    assert out["complete"] is True and out["yielded"] is False
    app._last_query_session = "another-chat"
    assert _call(provider, "review_claims", action="assist", ids=[1])["yielded"] is True


# ------------------------------------------------------------------ undo

def test_undo_batch_reverts_accept_reject_and_edit_as_events(ledger):
    old = core.propose(ledger, text="Ari's desk is oak.", kind="environment", origin="assistant_proposal",
                       proposer="assistant")["claim"]["id"]
    core.accept(ledger, old)
    new = core.propose(ledger, text="Ari's desk is walnut.", kind="environment", origin="assistant_proposal",
                       proposer="assistant", supersedes=old)["claim"]["id"]
    a, b, c, d = _seed(ledger, "Fact A.", "Fact B.", "Fact C.", "Fact D.")
    claims_before = ledger.execute("SELECT count(*) FROM claims").fetchone()[0]
    batch = "hermes:tg1:5"
    core.accept(ledger, new, batch=batch)  # retires the oak desk
    core.reject(ledger, a, batch=batch)
    edited = core.edit(ledger, b, text="Fact B, clean.", kind="taste", batch=batch)["id"]
    core.reject(ledger, c, batch="hermes:tg1:4")  # another message's batch: untouched
    assert core.batch_used(ledger, batch) == {"decisions": 3, "undone": False}
    out = core.undo_batch(ledger, batch, via="hermes:tg1:6")
    assert out["errors"] == []
    assert {r["id"]: r["undone"] for r in out["results"]} == {new: "accepted", a: "rejected", edited: "edited"}
    assert core.get(ledger, new)["review_status"] == "pending"
    assert core.get(ledger, old)["current"] and core.get(ledger, old)["review_status"] == "accepted"
    assert core.get(ledger, a)["review_status"] == "pending" and core.get(ledger, a)["review_note"] is None
    assert core.get(ledger, edited)["review_status"] == "rejected"
    assert core.get(ledger, b)["review_status"] == "pending" and core.get(ledger, b)["current"]
    assert core.get(ledger, c)["review_status"] == "rejected"
    # Nothing was deleted; the undo is on the record.
    assert ledger.execute("SELECT count(*) FROM claims").fetchone()[0] == claims_before + 1
    undone = core.claim_events(ledger, a, detail=True)[-1]
    assert undone["action"] == "review_undone"
    assert undone["detail"] == {"undo_of": batch, "via": "hermes:tg1:6", "was": "rejected"}
    assert core.batch_used(ledger, batch)["undone"] is True
    with pytest.raises(core.LedgerRejected, match="nothing_to_undo"):
        core.undo_batch(ledger, batch)
    # A claim that changed after its batch is left alone and reported.
    core.accept(ledger, d, batch="hermes:tg1:7")
    core.forget(ledger, d, reason="moved on", batch="hermes:tg1:8")
    assert core.undo_batch(ledger, "hermes:tg1:7")["errors"] == [{"id": d, "error": "changed_since"}]
    # A forget (or undo_auto) cannot be undone this way: refuse rather than revert something else.
    with pytest.raises(core.LedgerRejected, match="not_undoable"):
        core.undo_batch(ledger, "hermes:tg1:8")


# ------------------------------------------------------------------ approvals bound to the turn

@pytest.mark.parametrize("reply, parsed", [
    ("ok", {"assent": True, "drop": [], "edits": {}}),
    ("Ok, drop 3 and 5.", {"assent": True, "drop": [3, 5], "edits": {}}),
    ("да, удали 2 4", {"assent": True, "drop": [2, 4], "edits": {}}),
    ("drop 7", {"assent": False, "drop": [7], "edits": {}}),
    ("ok, drop 2, 3: Ari does not eat meat, ever.",  # a negation inside an edit's text is the owner's text
     {"assent": True, "drop": [2], "edits": {3: "Ari does not eat meat, ever."}}),
    ("yes\n2: First fix.\n5: Second fix.", {"assent": True, "drop": [], "edits": {2: "First fix.", 5: "Second fix."}}),
    ("don't do it", None), ("no, keep them", None), ("Not now.", None), ("ok but not 3", None),
    ("отмена, нет", None), ("ok, no", None), ("stop", None), ("skip", None), ("no", None),
    ("sure, whatever you think", None), ("undo", None), ("ok, drop", None),
    # Assent shapes beyond a single word; negation still refuses anywhere outside edits.
    ("👍", {"assent": True, "drop": [], "edits": {}}),
    ("✅", {"assent": True, "drop": [], "edits": {}}),
    ("ок 👍", {"assent": True, "drop": [], "edits": {}}),
    ("👍🏽, drop 4", {"assent": True, "drop": [4], "edits": {}}),
    ("yes please", {"assent": True, "drop": [], "edits": {}}),
    ("да, давай", {"assent": True, "drop": [], "edits": {}}),
    ("go", {"assent": True, "drop": [], "edits": {}}),
    ("Go ahead!", {"assent": True, "drop": [], "edits": {}}),
    ("давай", {"assent": True, "drop": [], "edits": {}}),
    ("применяй", {"assent": True, "drop": [], "edits": {}}),
    ("apply", {"assent": True, "drop": [], "edits": {}}),
    ("ok all", {"assent": True, "drop": [], "edits": {}}),
    ("да, все", {"assent": True, "drop": [], "edits": {}}),
    ("yes, all, drop 2", {"assent": True, "drop": [2], "edits": {}}),
    ("ок, всё", {"assent": True, "drop": [], "edits": {}}),
    ("👎", None), ("ok 🙄", None), ("please", None), ("all", None), ("go, no", None),
    ("не применяй", None), ("yes please, don't drop 2", None), ("👍 not now", None), ("да, нет", None),
])
def test_review_reply_grammar(reply, parsed):
    assert parse_review_reply(reply) == parsed


@pytest.mark.parametrize("reply", [
    "10:30 no, stop everything",  # a time, not an edit of item 10
    "1: wait, don't do anything", "3: no", "2: no, 3: yes", "3: нет", "3: ok", "4: drop",
    "ok\n3: new text; drop 4",  # a drop hidden in an edit's text
])
def test_replies_that_are_not_edits_parse_as_nothing(reply):
    assert parse_review_reply(reply) is None


def test_the_reply_is_bound_to_the_action(provider, served, state_db):
    app, _ = served
    with app.lock:
        ids = _seed(app.conn, "Claim one.", "Claim two.", "Claim three.")
    _suggest_all(provider, ids, "accept", kind="taste")

    def decide(action, quote, chosen=None, **kw):
        return _call(provider, "review_claims", action=action, ids=chosen or ids, approval_quote=quote, **kw)

    for reply in ("10:30 no, stop everything", "1: wait, don't do anything", "3: no", "2: no, 3: yes",
                  "ok\n3: new text; drop 4"):
        assistant_replies(state_db)
        say(state_db, reply)
        for action in ("apply", "accept", "reject"):
            assert decide(action, reply)["error"] == "approval_quote_not_found", (reply, action)
    assistant_replies(state_db)
    say(state_db, "drop 2")  # no assent: it rejects one claim, nothing else
    assert decide("apply", "drop 2")["error"] == "approval_not_for_action"
    assert decide("accept", "drop 2")["error"] == "approval_not_for_action"
    assert decide("reject", "drop 2", ids[:2])["error"] == "approval_not_for_action"  # more ids than drops
    assert decide("edit", "drop 2", ids[:1], text="New text.")["error"] == "approval_quote_not_found"
    assert decide("reject", "drop 2", ids[1:2])["results"][0]["review_status"] == "rejected"
    assistant_replies(state_db)
    say(state_db, "3: Claim three, reworded.")  # only an edit: it approves only that edit
    for action in ("apply", "accept", "reject"):
        assert decide(action, "3: Claim three, reworded.")["error"] == "approval_not_for_action"
    out = _call(provider, "review_claims", action="edit", id=ids[2], text="Claim three, reworded.",
                kind="taste", approval_quote="3: Claim three, reworded.")
    assert out["results"][0]["text"] == "Claim three, reworded."
    assistant_replies(state_db)
    say(state_db, "ok")  # an assent approves apply and accept, but drops nothing
    assert decide("reject", "ok", ids[:1])["error"] == "approval_not_for_action"
    assert decide("apply", "ok", ids[:1])["results"][0]["review_status"] == "accepted"


def test_expired_originals_bring_their_duplicates_back(ledger):
    t, d = _seed(ledger, "Original T.", "Copy of T.")
    core.suggest(ledger, t, action="accept", by="assistant", suggested_kind="taste")
    core.suggest(ledger, d, action="duplicate", by="assistant", duplicate_of=t)
    core.apply_suggestions(ledger, [d], batch="hermes:tg1:2")
    assert core.get(ledger, d)["review_status"] == "rejected"
    core.expire_pending(ledger, max_age_days=1, now="2026-12-01T00:00:00.000Z")  # T ages out
    assert core.get(ledger, t)["review_status"] == "expired"
    assert core.get(ledger, d)["review_status"] == "pending"  # back in review
    assert core.claim_events(ledger, d, detail=True)[-1]["detail"] == {"target_dropped": t}
    # Latest-wins: a newer proposal for the same slot replaces a pending original.
    old = core.propose(ledger, text="Ari's desk is oak.", kind="environment", origin="assistant_proposal",
                       proposer="assistant", subject_key="desk")["claim"]["id"]
    copy = core.propose(ledger, text="The desk is oak.", kind="environment", origin="assistant_proposal",
                        proposer="assistant")["claim"]["id"]
    core.suggest(ledger, old, action="accept", by="assistant")
    core.suggest(ledger, copy, action="duplicate", by="assistant", duplicate_of=old)
    core.apply_suggestions(ledger, [copy], batch="hermes:tg1:3")
    core.propose(ledger, text="Ari's desk is walnut.", kind="environment", origin="assistant_proposal",
                 proposer="assistant", subject_key="desk")
    assert core.get(ledger, old)["review_status"] == "expired"
    assert core.get(ledger, copy)["review_status"] == "pending"
    assert core.claim_events(ledger, copy, detail=True)[-1]["detail"] == {"target_dropped": old}


def test_short_assent_for_edits():
    assert all(is_assent(r) for r in ("ok", "👍", "yes please", "да, давай", "ok all"))
    assert not any(is_assent(r) for r in ("ok, drop 2", "3: text", "no", "👍 no", "please"))


def test_turn_approval_rules(state_db):
    ok = say(state_db, "ok")
    assert turn_approval(state_db, "tg1", "ok", "review") == ok
    assert turn_approval(state_db, "tg1", "ok", "undo") is None
    assert turn_approval(state_db, "tg1", "ok", "free") is None  # short free quotes only from a list
    assert turn_approval(state_db, "tg1", "ok", "free", is_assent) == ok
    assistant_works(state_db)
    assert turn_approval(state_db, "tg1", "ok", "review") == ok  # the turn is still running
    assistant_replies(state_db)
    assert turn_approval(state_db, "tg1", "ok", "review") is None  # stale: the turn ended
    say(state_db, "ok")
    say(state_db, "[kanban] task 7 woke up")  # an injected row after it
    assert turn_approval(state_db, "tg1", "ok", "review") is None
    say(state_db, "ok")
    say(state_db, "ignored", display_kind="internal_notification")
    assert turn_approval(state_db, "tg1", "ok", "review") is None
    round1 = say(state_db, "ok, drop 3 5")
    assert turn_approval(state_db, "tg1", "ok, drop 3 5", "review") == round1
    assert turn_approval(state_db, "tg1", "ok, drop 3", "review") is None  # the whole reply, not a part
    assistant_replies(state_db)
    say(state_db, "no, stop")
    assert turn_approval(state_db, "tg1", "ok, drop 3 5", "review") is None  # an older grammar reply
    assert turn_approval(state_db, "tg1", "no, stop", "free") is None  # a refusal approves nothing
    for reply in ("don't do it", "no, keep them", "Not now.", "ok but not 3", "отмена, нет", "no", "stop"):
        assistant_replies(state_db)
        say(state_db, reply)
        assert turn_approval(state_db, "tg1", reply, "review") is None, reply
        assert turn_approval(state_db, "tg1", reply, "undo") is None, reply
    for word in ("undo", "Отмени!", "верни"):
        assistant_replies(state_db)
        undo = say(state_db, word)
        assert turn_approval(state_db, "tg1", word, "undo") == undo
    assistant_replies(state_db)
    say(state_db, "отмена")  # cancel, not undo
    assert turn_approval(state_db, "tg1", "отмена", "undo") is None
    assistant_replies(state_db)
    long = say(state_db, "Please forget that I live in Brindle")
    assert turn_approval(state_db, "tg1", "forget that I live in Brindle", "free") == long
    assert turn_approval(state_db, "tg1", "forget that I live in Brindle", "review") is None
    assert turn_approval(state_db, "other", "forget that I live in Brindle", "free") is None
    assert previous_authored(state_db, say(state_db, "undo")) == long
    assert previous_authored(state_db.parent / "missing" / "state.db", long) is None  # refuses cleanly


def test_approval_bypasses_are_refused(provider, served, state_db):
    app, _ = served
    with app.lock:
        ids = _seed(app.conn, "Claim one.", "Claim two.")
        kept = core.propose(app.conn, text="Ari keeps a canoe.", kind="environment", origin="assistant_proposal",
                            proposer="assistant")["claim"]["id"]
        core.accept(app.conn, kept)
    _suggest_all(provider, ids, "reject")

    def apply(quote):
        return _call(provider, "review_claims", action="apply", ids=ids, approval_quote=quote)

    say(state_db, "ok")
    assistant_replies(state_db)
    assert apply("ok")["error"] == "approval_quote_not_found"  # stale ok
    say(state_db, "ok")
    say(state_db, "[Cron delivery: nightly]")
    assert apply("ok")["error"] == "approval_quote_not_found"  # injected row after it
    say(state_db, "ok, drop 3 5")
    assistant_replies(state_db)
    say(state_db, "no, stop")
    assert apply("ok, drop 3 5")["error"] == "approval_quote_not_found"  # older grammar reply
    assert apply("no, stop")["error"] == "approval_quote_not_found"
    for word in ("no", "stop"):
        assistant_replies(state_db)
        say(state_db, word)
        assert apply(word)["error"] == "approval_quote_not_found"
    assistant_replies(state_db)
    say(state_db, "ok")
    forget = _call(provider, "review_claims", action="forget", id=kept, reason="x", approval_quote="ok")
    assert forget["error"] == "approval_quote_not_found"  # forget needs a real quote
    assert _call(provider, "review_claims", action="undo", approval_quote="ok")["error"] == \
        "approval_quote_not_found"
    with app.lock:
        assert {core.get(app.conn, i)["review_status"] for i in ids} == {"pending"}


def test_guided_round_shares_one_batch_and_undo_reverts_it(provider, served, state_db):
    app, _ = served
    with app.lock:
        ids = _seed(app.conn, "Claim one.", "Claim two.", "Claim three.", "Claim four.")
    _suggest_all(provider, ids, "accept", kind="preference", topic="Work")
    d = _call(provider, "review_claims", action="digest")
    assert d["ok"] and d["plan"]["to_read"] == 4 and d["group"]["topic"] == "Work"
    n = {i["n"]: i["id"] for i in d["group"]["items"]}
    reply = "ok, drop 2, 3: Claim three, reworded."
    locator = say(state_db, reply)
    applied = _call(provider, "review_claims", action="apply", ids=[n[1], n[4]], approval_quote=reply)
    assistant_works(state_db)
    dropped = _call(provider, "review_claims", action="reject", ids=[n[2]], approval_quote=reply)
    edited = _call(provider, "review_claims", action="edit", id=n[3], text="Claim three, reworded.",
                   kind="preference", approval_quote=reply)
    assert applied["approval_locator"] == dropped["approval_locator"] == edited["approval_locator"] == locator
    assert [r["review_status"] for r in applied["results"]] == ["accepted", "accepted"]
    assert _call(provider, "review_claims", action="summary")["pending"] == 0
    assistant_replies(state_db, "Done: 2 remembered, 1 dropped, 1 edited. 0 left.")
    say(state_db, "undo")
    out = _call(provider, "review_claims", action="undo", approval_quote="undo")
    assert out["ok"] and out["batch"] == locator and not out["errors"] and len(out["results"]) == 4
    assert out["core_stale"] is True  # accepts were reverted: the published core needs a recompile
    assert _call(provider, "review_claims", action="summary")["presort"] == {"accept": 4}
    assert _call(provider, "review_claims", action="undo", approval_quote="undo")["error"] == "nothing_to_undo"
    provider._context = "cron"
    assert _call(provider, "review_claims", action="undo", approval_quote="undo")["error"] == \
        "review_requires_primary_session"


def test_undo_reverts_only_the_message_right_before_it(provider, served, state_db):
    app, _ = served
    with app.lock:
        a, b = _seed(app.conn, "Claim A.", "Claim B.")
        kept = core.propose(app.conn, text="Ari keeps a canoe.", kind="environment", origin="assistant_proposal",
                            proposer="assistant")["claim"]["id"]
        core.accept(app.conn, kept)
    _suggest_all(provider, [a, b], "reject")
    say(state_db, "ok")
    assert _call(provider, "review_claims", action="apply", ids=[a], approval_quote="ok")["results"]
    assistant_replies(state_db)
    say(state_db, "thanks, looks good")
    assistant_replies(state_db)
    say(state_db, "undo")
    # The message before "undo" decided nothing: an older batch is never reached for.
    assert _call(provider, "review_claims", action="undo", approval_quote="undo")["error"] == "nothing_to_undo"
    assistant_replies(state_db)
    say(state_db, "forget the canoe claim please")
    assert _call(provider, "review_claims", action="forget", id=kept, reason="sold",
                 approval_quote="forget the canoe claim please")["results"]
    assistant_replies(state_db)
    say(state_db, "undo")
    assert _call(provider, "review_claims", action="undo", approval_quote="undo")["error"] == "not_undoable"
    with app.lock:
        assert core.get(app.conn, a)["review_status"] == "rejected"


def test_bucket_apply_needs_the_digest_stamp_and_undo_covers_it(provider, served, state_db):
    app, _ = served
    with app.lock:
        keep, dup1, dup2, cov, late = _seed(app.conn, "Kept fact.", "Kept fact again.", "Kept, once more.",
                                            "Covered fact.", "Late duplicate.")
    _call(provider, "review_claims", action="suggest", id=keep, suggestion="accept", kind="taste")
    _suggest_all(provider, [dup1, dup2], "duplicate", duplicate_of=keep)
    _call(provider, "review_claims", action="suggest", id=cov, suggestion="covered",
          evidence_locator="PROJECTS/Garden.md:1-3")
    shown = _call(provider, "review_claims", action="digest", bucket="duplicate")
    stamp = shown["buckets"]["duplicate"]["stamp"]
    assert shown["buckets"]["duplicate"]["count"] == 2 and shown["buckets"]["covered"]["count"] == 1
    reply = "ok"
    say(state_db, reply)
    assert _call(provider, "review_claims", action="apply", bucket="accept", bucket_stamp=stamp,
                 approval_quote=reply)["error"] == "unknown_bucket"  # never applied blind
    assert _call(provider, "review_claims", action="apply", bucket="duplicate", ids=[dup1], bucket_stamp=stamp,
                 approval_quote=reply)["error"] == "ids_or_bucket"
    assert _call(provider, "review_claims", action="apply", bucket="duplicate",
                 approval_quote=reply)["error"] == "bucket_stamp_required"
    # Another context re-sorts a claim into the bucket after Ari saw it: the approval does not widen.
    _call(provider, "review_claims", action="suggest", id=late, suggestion="duplicate", duplicate_of=keep)
    assert _call(provider, "review_claims", action="apply", bucket="duplicate", bucket_stamp=stamp,
                 approval_quote=reply)["error"] == "bucket_changed"
    _call(provider, "review_claims", action="suggest", id=dup2, suggestion="keep")
    assert _call(provider, "review_claims", action="apply", bucket="duplicate", bucket_stamp=stamp,
                 approval_quote=reply)["error"] == "bucket_changed"  # same count, other claims
    stamp = _call(provider, "review_claims", action="digest")["buckets"]["duplicate"]["stamp"]
    out = _call(provider, "review_claims", action="apply", bucket="duplicate", bucket_stamp=stamp,
                approval_quote=reply)
    assert out["ok"] and out["bucket"] == "duplicate" and not out["errors"]
    assert [(r["id"], r["applied"], r["text"]) for r in out["results"]] == [
        (dup1, "duplicate", "Kept fact again."), (late, "duplicate", "Late duplicate.")]
    assistant_replies(state_db)
    say(state_db, "undo")
    undone = _call(provider, "review_claims", action="undo", approval_quote="undo")
    assert sorted(r["id"] for r in undone["results"]) == [dup1, late] and undone["core_stale"] is False
    assert _call(provider, "review_claims", action="summary")["presort"] == {
        "accept": 1, "covered": 1, "duplicate": 2, "keep": 1}


def test_one_approval_decides_at_most_the_cap(provider, served, state_db):
    app, _ = served
    cap = core.BUCKET_CAP
    with app.lock:
        ids = _seed(app.conn, *[f"Junk number {i}." for i in range(cap + 5)])
        for cid in ids:
            core.suggest(app.conn, cid, action="reject", by="assistant")
    stamp = _call(provider, "review_claims", action="digest")["buckets"]["reject"]["stamp"]
    reply = "yes"
    say(state_db, reply)
    assert _call(provider, "review_claims", action="apply", bucket="reject", bucket_stamp=stamp,
                 approval_quote=reply)["error"] == "bucket_too_large"
    first = _call(provider, "review_claims", action="apply", ids=ids[:cap], approval_quote=reply)
    assert len(first["results"]) == cap
    assert _call(provider, "review_claims", action="apply", ids=ids[cap:], approval_quote=reply)["error"] == \
        "approval_cap_reached"


# ------------------------------------------------------------------ assist

def test_assist_finds_near_duplicates_and_vault_coverage(provider, served):
    app, _ = served
    with app.lock:
        core.propose(app.conn, text="Ari keeps a kayak in the shed.", kind="environment",
                     origin="assistant_proposal", proposer="assistant")
        accepted = core.pending_current(app.conn)[0][0]
        core.accept(app.conn, accepted)
        garden, dup, other, kayak = _seed(
            app.conn, "Tomatoes and basil grow well in the south bed.",
            "Tomatoes and basil grow very well in the south bed.", "Ari meditates every morning.",
            "Ari keeps the kayak in the shed.")
    app.assist_min_score = 0.8  # hash-ngram test vectors; bge-m3 default is 0.85
    out = _call(provider, "review_claims", action="assist", limit=2)
    assert out["ok"] and out["complete"] is True and out["yielded"] is False  # this page is done
    assert out["vectors"] == {"embedded": 5, "missing": 0} and out["next_offset"] == 2
    assert {"pending": [garden, dup], "accepted": []} in out["clusters"]
    assert {"pending": [kayak], "accepted": [accepted]} in out["clusters"]
    first = {c["id"]: c for c in out["claims"]}
    assert [n["id"] for n in first[garden]["near"]] == [dup] and first[garden]["near"][0]["score"] >= 0.8
    vault = first[garden]["vault"]
    assert vault["locator"].startswith("PROJECTS/Garden.md") and vault["score"] > 0 and "basil" in vault["excerpt"]
    page2 = _call(provider, "review_claims", action="assist", offset=2, limit=2)
    assert [c["id"] for c in page2["claims"]] == [other, kayak] and page2["next_offset"] is None
    assert page2["complete"] is True
    picked = _call(provider, "review_claims", action="assist", ids=[kayak])
    assert picked["claims"][0]["near"] == [{"id": accepted, "score": picked["claims"][0]["near"][0]["score"],
                                            "status": "accepted"}]
    # Bulk lookups stay out of the serving query-vector cache.
    with app.lock:
        texts = [t for _, t in core.pending_current(app.conn)]
    assert not any(key[1] in texts for key in app.evidence.queries._vectors)


def test_assist_yields_to_live_turns_and_lane_ticks(provider, served):
    app, _ = served
    with app.lock:
        _seed(app.conn, "Some fact.", "Another fact.")
    app._last_query_at = time.monotonic()  # a live turn just queried
    out = _call(provider, "review_claims", action="assist")
    assert out["ok"] and out["complete"] is False and out["yielded"] is True
    assert out["vectors"]["missing"] == 2 and len(out["vault_unfinished"]) == 2
    app._last_query_at = float("-inf")
    app.assist_budget = 0.3
    assert TICK_LOCK.acquire()
    try:  # a lane's background tick holds the CPU: the assist does not embed beside it
        held = _call(provider, "review_claims", action="assist")
    finally:
        TICK_LOCK.release()
    assert held["complete"] is False and held["vectors"]["missing"] == 2
    app.assist_budget = 5.0
    assert app._assist_lock.acquire()
    try:
        assert _call(provider, "review_claims", action="assist") == {"ok": False, "error": "assist_busy"}
    finally:
        app._assist_lock.release()
    assert _call(provider, "review_claims", action="assist")["complete"] is True


def test_review_index_reuses_serving_vectors():
    class Embedder:
        def __init__(self):
            self.calls = []

        def embed_passages(self, texts):
            self.calls.append(list(texts))
            return [np.ones(4, dtype=np.float32) for _ in texts]

    serving, emb = ClaimIndex(), Embedder()
    serving.sync([(1, "accepted fact")], emb)
    review = ClaimIndex(min_similarity=-1.0)
    review.sync([(2, "pending fact"), (1, "accepted fact"), (3, "edited")], emb, reuse=serving)
    assert emb.calls == [["accepted fact"], ["pending fact", "edited"]] and len(review) == 3
    review.sync([(1, "accepted fact, changed")], emb, reuse=serving)  # a different text is embedded
    assert emb.calls[-1] == ["accepted fact, changed"]


# ------------------------------------------------------------------ /quoth queue

def test_queue_view_is_a_digest_and_groups_fit_one_message(provider, served):
    app, _ = served
    plugin = sys.modules["_quoth_test_plugin"]
    with app.lock:
        tea, walk, junk = _seed(app.conn, "Ari drinks green tea.", "Ari walks daily.", "Weather once.")
        core.suggest(app.conn, tea, action="accept", by="assistant", suggested_kind="preference",
                     topic="Food & health", rewrite="Ari prefers green tea.")
        core.suggest(app.conn, walk, action="covered", by="assistant", topic="Food & health",
                     evidence_locator="HEALTH/walks.md:2-4")
        core.suggest(app.conn, junk, action="reject", by="assistant")
    view = plugin.handle_quoth_command("queue")
    assert view.startswith("Memory review: 3 waiting")
    assert "remember 1 · already in your notes 1 · drop 1" in view
    assert "By topic: Food & health 2 · Other 1" in view and "Say 'review memory'" in view
    food = plugin.handle_quoth_command("queue food & health")
    assert food.splitlines()[:3] == ["Food & health: 2 waiting", f"#{tea} Ari prefers green tea. — remember",
                                     f"#{walk} Ari walks daily. — already in your notes"]
    assert "[" not in food  # no internal tags
    assert f"#{junk} Weather once. — drop" in plugin.handle_quoth_command("queue drop")
    assert "presort: covered (Food & health), in notes: HEALTH/walks.md:2-4" in \
        plugin.handle_quoth_command(f"item {walk}")
    with app.lock:
        for cid in _seed(app.conn, *[f"Long synthetic claim {i} " + "word " * 100 for i in range(60)]):
            core.suggest(app.conn, cid, action="reject", by="assistant")
    many = plugin.handle_quoth_command("queue reject")
    assert len(many) <= 3800 and "more." in many


# ------------------------------------------------------------------ plugin schema and skill

def test_review_schema_lists_the_guided_review_actions(monkeypatch):
    plugin = _load_plugin(monkeypatch, REPO / "hermes_plugin" / "quoth")
    schema = plugin.REVIEW_SCHEMA
    props = schema["parameters"]["properties"]
    assert {"digest", "assist", "undo"} <= set(props["action"]["enum"])
    assert "covered" in props["suggestion"]["enum"]
    assert set(props["bucket"]["enum"]) >= {"duplicate", "covered", "stale", "reject"}
    assert props["topic"]["maxLength"] == core.MAX_TOPIC_CHARS and "evidence_locator" in props
    assert len(schema["description"]) < 1000  # keep tool descriptions short
    sent = {}

    class Client:
        def post(self, route, body, timeout):
            sent.update(body)
            return {"ok": True}

    p = plugin.QuothMemoryProvider("/nonexistent.sock")
    p._client = Client()
    p.handle_tool_call("review_claims", {"action": "suggest", "id": 1, "suggestion": "covered",
                                         "topic": "Home", "evidence_locator": "A.md:1", "bucket": "stale",
                                         "bucket_stamp": "3:9"})
    assert {k: sent[k] for k in ("topic", "evidence_locator", "bucket", "bucket_stamp")} == {
        "topic": "Home", "evidence_locator": "A.md:1", "bucket": "stale", "bucket_stamp": "3:9"}


def test_review_skill_is_the_guided_flow():
    skill = (REPO / "hermes_plugin" / "quoth" / "skills" / "memory-review" / "SKILL.md").read_text()
    for phrase in ("action=digest", "action=assist", "bucket=", "action=undo", "approval_quote",
                   '"ok"', '"skip"', '"stop"', '"undo"', "evidence_locator"):
        assert phrase in skill, phrase
    assert "version: 3." in skill
