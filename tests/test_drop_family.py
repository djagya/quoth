"""Dropping a reviewed memory drops its confirmed duplicates, atomically, under the same approval.

The family of a dropped claim is every claim the owner confirmed as its copy (rejected as a
duplicate of a member), transitively. Synthetic claims only."""
from __future__ import annotations

import pytest

from quoth.ledger import core
from test_guided_review import T0, _seed, assistant_replies, ledger, say, state_db  # noqa: F401 (fixtures)
from test_sidecar import _call, provider, served, snapshot_db  # noqa: F401 (fixtures)


def _snapshot(conn) -> tuple[list[tuple], int]:
    """Every claim row and the event count: what 'zero writes' compares."""
    claims = [tuple(r) for r in conn.execute("SELECT * FROM claims ORDER BY id")]
    return claims, conn.execute("SELECT count(*) FROM ledger_events").fetchone()[0]


def _status(conn, *ids) -> list[str]:
    return [core.get(conn, cid)["review_status"] for cid in ids]


def _turn(state_db, reply):
    """The owner's next reply, opening a new turn."""
    assistant_replies(state_db)
    return say(state_db, reply)


def _confirm(provider, state_db, copies, target, reply="ok"):
    """Presort `copies` as duplicates of `target` (suggested accept) and apply them on the owner's ok."""
    _call(provider, "review_claims", action="suggest", id=target, suggestion="accept", kind="taste")
    for cid in copies:
        _call(provider, "review_claims", action="suggest", id=cid, suggestion="duplicate", duplicate_of=target)
    _turn(state_db, reply)
    out = _call(provider, "review_claims", action="apply", ids=list(copies), approval_quote=reply)
    assert [r["review_status"] for r in out["results"]] == ["rejected"] * len(copies), out


def _taste(conn, text):
    """A pending claim with a kind already set: accepting it is a plain accept, not an edit."""
    return core.propose(conn, text=text, kind="taste", origin="assistant_proposal", proposer="assistant",
                        now=T0)["claim"]["id"]


def _legacy_restore(conn, target, batch="hermes:tg1:legacy"):
    """What a release before this one did when the owner dropped `target`: its copies came back."""
    with core._tx(conn):
        return core._restore_duplicates_of(conn, target, "owner", T0, batch)


def test_drop_takes_its_confirmed_copies_and_their_copies(provider, served, state_db):
    app, _ = served
    with app.lock:
        x, c1, c2, d, s = _seed(app.conn, "Original X.", "Copy one of X.", "Copy two of X.",
                                "Copy of copy one.", "Similar to X.")
        u = _taste(app.conn, "Unrelated fact.")
    _confirm(provider, state_db, [d], c1)  # D folds into C1 while C1 is still meant to stay
    _confirm(provider, state_db, [c1, c2], x)  # then C1 and C2 fold into X
    _call(provider, "review_claims", action="suggest", id=u, suggestion="accept")
    _call(provider, "review_claims", action="apply", ids=[u], approval_quote="ok")
    # S is only suggested as X's duplicate: not a confirmation, so it stays pending.
    _call(provider, "review_claims", action="suggest", id=s, suggestion="duplicate", duplicate_of=x)
    with app.lock:
        accepted_before = core.get(app.conn, u)
        assert accepted_before["review_status"] == "accepted" and accepted_before["current"]
        _, events_before = _snapshot(app.conn)
    reply = "ok, drop 1"
    locator = _turn(state_db, reply)
    out = _call(provider, "review_claims", action="reject", ids=[x], approval_quote=reply)
    assert out["errors"] == [] and out["results"][0]["review_status"] == "rejected"
    assert out["results"][0]["family"] == {"root": x, "ids": [c1, c2, d], "rejected_now": [],
                                           "already_rejected": [c1, c2, d]}
    with app.lock:
        assert _status(app.conn, x, c1, c2, d) == ["rejected"] * 4
        assert _status(app.conn, s) == ["pending"]
        assert core.get(app.conn, u) == accepted_before  # an unrelated accepted claim is untouched
        assert _snapshot(app.conn)[1] == events_before + 1  # only X's own rejection was written
        # Readback: no confirmed family member is pending.
        family = core.duplicate_family(app.conn, x)
        assert family["to_reject"] == [] and "pending" not in _status(app.conn, *family["already_rejected"])
        assert core.batch_used(app.conn, locator)["decisions"] == 1


def test_restored_copies_are_dropped_again_and_undo_brings_them_back(provider, served, state_db):
    app, _ = served
    with app.lock:
        x, c, r, b = _seed(app.conn, "Original X.", "Kept-dropped copy.", "Restored copy.",
                           "Copy dropped for another reason.")
        a = _taste(app.conn, "Copy the owner kept.")
    _confirm(provider, state_db, [r, a, b], x)
    with app.lock:  # an older release restored them when X was dropped; X was later put back in review
        assert _legacy_restore(app.conn, x) == [r, b, a]
    _confirm(provider, state_db, [c], x)
    # The owner decides A and B again after the restore: they leave X's family.
    _call(provider, "review_claims", action="suggest", id=a, suggestion="accept")
    _call(provider, "review_claims", action="apply", ids=[a], approval_quote="ok")
    _turn(state_db, "drop 1")
    _call(provider, "review_claims", action="reject", ids=[b], approval_quote="drop 1")
    with app.lock:
        assert _status(app.conn, x, c, r, a, b) == ["pending", "rejected", "pending", "accepted", "rejected"]
        a_before, b_events = core.get(app.conn, a), len(core.claim_events(app.conn, b))
        assert core.family_rejects(app.conn, [x]) == {r}

    reply = "ok, drop 1"
    locator = _turn(state_db, reply)
    out = _call(provider, "review_claims", action="reject", ids=[x], approval_quote=reply)
    assert out["results"][0]["family"] == {"root": x, "ids": [c, r], "rejected_now": [r],
                                           "already_rejected": [c]}
    with app.lock:
        assert _status(app.conn, x, c, r, a, b) == ["rejected", "rejected", "rejected", "accepted", "rejected"]
        assert core.claim_events(app.conn, r, detail=True)[-1]["detail"] == {
            "batch": locator, "duplicate_of": x, "family_of": x}
        assert core.get(app.conn, r)["review_note"].startswith(f"duplicate of #{x}, dropped with #{x}")
        assert core.get(app.conn, a) == a_before and len(core.claim_events(app.conn, b)) == b_events
        assert core.duplicate_family(app.conn, x)["to_reject"] == []  # readback
        # The re-reject counts as a decision of the same approving message.
        assert core.batch_used(app.conn, locator)["decisions"] == 2
        before = _snapshot(app.conn)
    # A repeat is refused per id and writes nothing.
    again = _call(provider, "review_claims", action="reject", ids=[x], approval_quote=reply)
    assert again["results"] == [] and again["errors"] == [{"id": x, "error": "not_pending"}]
    with app.lock:
        assert _snapshot(app.conn) == before

    _turn(state_db, "undo")
    undone = _call(provider, "review_claims", action="undo", approval_quote="undo")
    assert sorted(res["id"] for res in undone["results"]) == sorted([x, r]) and undone["errors"] == []
    with app.lock:  # X and the copy it re-dropped come back; the copy dropped earlier stays dropped
        assert _status(app.conn, x, c, r, a, b) == ["pending", "rejected", "pending", "accepted", "rejected"]


def test_a_free_text_or_stale_approval_writes_nothing(provider, served, state_db):
    app, _ = served
    with app.lock:
        x, r = _seed(app.conn, "Original X.", "Restored copy.")
    _confirm(provider, state_db, [r], x)
    with app.lock:
        _legacy_restore(app.conn, x)
        before = _snapshot(app.conn)
    rule = "Dropping a memory must drop its duplicates too."
    _turn(state_db, rule)
    for ids in ([r], [x]):
        out = _call(provider, "review_claims", action="reject", ids=ids, approval_quote=rule)
        assert out["error"] == "approval_quote_not_found"
    _turn(state_db, "ok, drop 1")
    assistant_replies(state_db)  # the turn ended: the reply no longer approves anything
    out = _call(provider, "review_claims", action="reject", ids=[x], approval_quote="ok, drop 1")
    assert out["error"] == "approval_quote_not_found"
    with app.lock:
        assert _snapshot(app.conn) == before


def test_the_approval_cap_counts_family_rejects(provider, served, state_db, monkeypatch):
    app, _ = served
    with app.lock:
        x, r1, r2 = _seed(app.conn, "Original X.", "Restored copy one.", "Restored copy two.")
    _confirm(provider, state_db, [r1, r2], x)
    with app.lock:
        _legacy_restore(app.conn, x)
        before = _snapshot(app.conn)
    monkeypatch.setattr(core, "BUCKET_CAP", 2)  # X alone fits; X with its two copies does not
    _turn(state_db, "ok, drop 1")
    out = _call(provider, "review_claims", action="reject", ids=[x], approval_quote="ok, drop 1")
    assert out["error"] == "approval_cap_reached"
    _call(provider, "review_claims", action="suggest", id=x, suggestion="reject")
    _turn(state_db, "ok")
    assert _call(provider, "review_claims", action="apply", ids=[x], approval_quote="ok")["error"] \
        == "approval_cap_reached"
    with app.lock:
        assert _snapshot(app.conn)[0] == before[0]  # only the suggestion event was added
    monkeypatch.setattr(core, "BUCKET_CAP", 3)
    out = _call(provider, "review_claims", action="apply", ids=[x], approval_quote="ok")
    assert out["results"][0]["family"]["rejected_now"] == [r1, r2]


def test_a_cycle_ends(ledger):
    x, a, b = _seed(ledger, "Original X.", "Copy A.", "Copy B.")
    core.reject(ledger, a, duplicate_of=x, batch="hermes:tg1:2")
    core.reject(ledger, b, duplicate_of=a, batch="hermes:tg1:2")
    with core._tx(ledger):  # a corrupt history: X itself restored as a copy of its own copy B
        core._event(ledger, x, "restored", "owner", T0, target_dropped=b)
    out = core.reject(ledger, x, batch="hermes:tg1:3")
    assert out["family"] == {"root": x, "ids": [a, b], "rejected_now": [], "already_rejected": [a, b]}


def test_a_failure_mid_family_rolls_everything_back(ledger, monkeypatch):
    x, r1, r2 = _seed(ledger, "Original X.", "Restored copy one.", "Restored copy two.")
    for cid in (r1, r2):
        core.reject(ledger, cid, duplicate_of=x, batch="hermes:tg1:2")
    _legacy_restore(ledger, x)
    before = _snapshot(ledger)
    real, written = core._event, []

    def failing(conn, claim_id, action, *args, **detail):
        if detail.get("family_of") is not None:
            written.append(claim_id)
            if len(written) == 2:
                raise RuntimeError("disk full")
        return real(conn, claim_id, action, *args, **detail)

    monkeypatch.setattr(core, "_event", failing)
    with pytest.raises(RuntimeError):
        core.reject(ledger, x, batch="hermes:tg1:3")
    assert written == [r1, r2] and _snapshot(ledger) == before
    assert _status(ledger, x, r1, r2) == ["pending"] * 3


def test_expiry_still_restores_copies(ledger):
    """The owner never decided an expired claim: its copies return to review (`target_dropped`)."""
    x, c = _seed(ledger, "Original X.", "Copy of X.")
    core.reject(ledger, c, duplicate_of=x, batch="hermes:tg1:2")
    core.expire_pending(ledger, max_age_days=1, now="2026-12-01T00:00:00.000Z")
    assert _status(ledger, x, c) == ["expired", "pending"]
    # C is pending through a restore, but X is not a dropped claim: no family to take it.
    assert core.duplicate_family(ledger, c)["to_reject"] == []
