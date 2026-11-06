"""Copies left pending by an owner's drop in releases before 0.18.1 clear in one step.

Before 0.18.1, when the owner rejected an original X, the ledger restored X's confirmed copies
to pending. Those copies sit in the `dropped_copies` junk bucket until the owner clears it.
Expiry by age and latest-wins also restore copies, but X then ended without the owner's
decision, so those copies stay in review. Synthetic claims only."""
from __future__ import annotations

from quoth.ledger import core
from test_guided_review import T0, _seed, assistant_replies, ledger, say, state_db  # noqa: F401 (fixtures)
from test_sidecar import _call, provider, served, snapshot_db  # noqa: F401 (fixtures)

LEGACY = "hermes:tg1:legacy"


def _confirm_copies(conn, original, *copies, batch="hermes:tg1:confirm"):
    """The owner drops each copy as a duplicate of `original` (a confirmed copy)."""
    for cid in copies:
        core.reject(conn, cid, duplicate_of=original, batch=batch)


def _legacy_drop(conn, original, batch=LEGACY):
    """What a release before 0.18.1 did when the owner dropped `original`: its copies came back."""
    core.reject(conn, original, batch=batch)
    with core._tx(conn):
        return core._restore_duplicates_of(conn, original, "owner", T0, batch)


def _ids(conn) -> list[int]:
    return [c["id"] for c in core.dropped_copies(conn)]


def _status(conn, *ids) -> list[str]:
    return [core.get(conn, cid)["review_status"] for cid in ids]


def _turn(state_db, reply):
    assistant_replies(state_db)
    return say(state_db, reply)


# ------------------------------------------------------------------ which copies are orphans

def test_only_copies_of_an_owner_drop_are_dropped_copies(ledger):
    x, c1, c2 = _seed(ledger, "Original X.", "Copy one of X.", "Copy two of X.")
    _confirm_copies(ledger, x, c1, c2)
    assert _legacy_drop(ledger, x) == [c1, c2]
    assert core.dropped_copies(ledger) == [
        {"id": c1, "copy_of": x, "event": core._last_decision_event(ledger, c1)[0]},
        {"id": c2, "copy_of": x, "event": core._last_decision_event(ledger, c2)[0]}]

    # Expiry by age: Y ended without the owner's decision, so its copy is not an orphan.
    y = core.propose(ledger, text="Old original Y.", kind="taste", origin="assistant_proposal",
                     proposer="assistant", now="2026-08-01T09:00:00.000Z")["claim"]["id"]
    (d,) = _seed(ledger, "Copy of Y.")
    _confirm_copies(ledger, y, d)
    assert core.expire_pending(ledger, 30, now="2026-09-15T00:00:00.000Z") == [y]
    # Latest-wins: a newer proposal for Z's slot replaced it, again not the owner's decision.
    z = core.propose(ledger, text="Original Z.", kind="taste", origin="assistant_proposal",
                     proposer="assistant", subject_key="slot", now=T0)["claim"]["id"]
    (e,) = _seed(ledger, "Copy of Z.")
    _confirm_copies(ledger, z, e)
    core.propose(ledger, text="Newer Z.", kind="taste", origin="assistant_proposal", proposer="assistant",
                 subject_key="slot", now=T0)
    assert _status(ledger, d, e) == ["pending", "pending"]  # both restored to review
    assert [ev["action"] for ev in core.claim_events(ledger, d)][-1] == "restored"
    assert [ev["action"] for ev in core.claim_events(ledger, e)][-1] == "restored"
    assert _ids(ledger) == [c1, c2]

    # A presort suggestion is no decision: C1 stays an orphan, and leaves the reject bucket.
    core.suggest(ledger, c1, action="reject", by="assistant")
    assert core.bucket_state(ledger, "reject")[0] == []
    assert core.bucket_state(ledger, "dropped_copies")[0] == [c1, c2]
    # The owner decides C2 again (dropped, then undone): it is theirs to review now.
    core.reject(ledger, c2, batch="hermes:tg1:again")
    core.undo_batch(ledger, "hermes:tg1:again")
    assert _status(ledger, c2) == ["pending"] and _ids(ledger) == [c1]
    # The owner keeps C1 (an accepting edit sets its kind): no longer pending, no longer an orphan.
    core.edit(ledger, c1, kind="taste")
    assert _ids(ledger) == []


def test_undoing_the_original_drop_returns_its_copies_to_review(ledger):
    x, c = _seed(ledger, "Original X.", "Copy of X.")
    _confirm_copies(ledger, x, c)
    _legacy_drop(ledger, x)
    assert _ids(ledger) == [c]
    core.undo_batch(ledger, LEGACY)  # X is pending again: the owner has not dropped it after all
    assert _status(ledger, x, c) == ["pending", "pending"] and _ids(ledger) == []


def test_digest_and_health_count_dropped_copies_once(ledger):
    x, c1, c2, j = _seed(ledger, "Original X.", "Copy one of X.", "Copy two of X.", "Junk.")
    _confirm_copies(ledger, x, c1, c2)
    _legacy_drop(ledger, x)
    core.suggest(ledger, c1, action="reject", by="assistant", topic="Home")
    core.suggest(ledger, c2, action="accept", by="assistant", topic="Home", suggested_kind="taste")
    core.suggest(ledger, j, action="reject", by="assistant", topic="Home")
    d = core.digest(ledger)
    assert {b: v["count"] for b, v in d["buckets"].items()} == {
        "duplicate": 0, "covered": 0, "stale": 0, "reject": 1, "dropped_copies": 2}
    assert d["plan"]["cleanup"] == 3 and d["plan"]["to_read"] == 0 and d["pending"] == 3
    assert d["topics"][0]["to_read"] == 0  # C2's accept suggestion is not read: it is a copy
    group = core.digest(ledger, bucket="dropped_copies")["group"]
    assert group["bucket"] == "dropped_copies" and group["total"] == 2
    assert [(i["id"], i["copy_of"], i["copy_of_text"]) for i in group["items"]] == [
        (c1, x, "Original X."), (c2, x, "Original X.")]
    assert core.health(ledger)["dropped_copies"] == 2


# ------------------------------------------------------------------ the bucket through review

def test_bucket_apply_needs_its_stamp_rejects_as_duplicates_and_undo_restores(provider, served, state_db):
    app, _ = served
    with app.lock:
        x, c1, c2, other = _seed(app.conn, "Original X.", "Copy one of X.", "Copy two of X.", "Other.")
        _confirm_copies(app.conn, x, c1, c2)
        _legacy_drop(app.conn, x)
    shown = _call(provider, "review_claims", action="digest", bucket="dropped_copies")
    assert shown["buckets"]["dropped_copies"]["count"] == 2
    assert [i["id"] for i in shown["group"]["items"]] == [c1, c2]
    stamp = shown["buckets"]["dropped_copies"]["stamp"]
    assert app.health()["ledger"]["dropped_copies"] == 2

    reply = "ok"
    _turn(state_db, reply)
    assert _call(provider, "review_claims", action="apply", bucket="dropped_copies",
                 approval_quote=reply)["error"] == "bucket_stamp_required"
    # The bucket moves after the owner saw it (C2 decided again): the approval does not apply.
    with app.lock:
        core.reject(app.conn, c2, batch="hermes:tg1:other")
        core.undo_batch(app.conn, "hermes:tg1:other")
    assert _call(provider, "review_claims", action="apply", bucket="dropped_copies", bucket_stamp=stamp,
                 approval_quote=reply)["error"] == "bucket_changed"
    with app.lock:  # back to two copies: C2 is restored again by the legacy path
        core.reject(app.conn, c2, duplicate_of=x, batch="hermes:tg1:other2")
        with core._tx(app.conn):
            core._restore_duplicates_of(app.conn, x, "owner", T0, LEGACY)
        events_before = app.conn.execute("SELECT count(*) FROM ledger_events").fetchone()[0]
    stamp = _call(provider, "review_claims", action="digest")["buckets"]["dropped_copies"]["stamp"]
    reply = "yes"
    locator = _turn(state_db, reply)
    out = _call(provider, "review_claims", action="apply", bucket="dropped_copies", bucket_stamp=stamp,
                approval_quote=reply)
    assert out["ok"] and out["bucket"] == "dropped_copies" and out["errors"] == [] and "core" not in out
    assert [(r["id"], r["applied"], r["duplicate_of"], r["review_status"]) for r in out["results"]] == [
        (c1, "dropped_copies", x, "rejected"), (c2, "dropped_copies", x, "rejected")]
    with app.lock:
        assert _status(app.conn, x, c1, c2, other) == ["rejected", "rejected", "rejected", "pending"]
        for cid in (c1, c2):
            last = core.claim_events(app.conn, cid, detail=True)[-1]
            assert (last["action"], last["detail"]) == ("rejected", {"batch": locator, "duplicate_of": x})
            assert core.get(app.conn, cid)["review_note"].startswith(f"duplicate of #{x}, which the owner dropped")
        assert app.conn.execute("SELECT count(*) FROM ledger_events").fetchone()[0] == events_before + 2
        assert core.duplicate_family(app.conn, x) == {"root": x, "already_rejected": [c1, c2], "to_reject": []}
        assert core.batch_used(app.conn, locator)["decisions"] == 2
    assert app.health()["ledger"]["dropped_copies"] == 0

    # Undo returns both to pending; the owner has now decided them, so they are not re-offered.
    _turn(state_db, "undo")
    undone = _call(provider, "review_claims", action="undo", approval_quote="undo")
    assert sorted(r["id"] for r in undone["results"]) == [c1, c2] and undone["core_stale"] is False
    with app.lock:
        assert _status(app.conn, c1, c2) == ["pending", "pending"] and _ids(app.conn) == []
    # A stale id passed straight to the ledger is refused and left alone.
    with app.lock:
        assert core.clear_dropped_copies(app.conn, [c1, other]) == [
            {"id": c1, "error": "not_a_dropped_copy"}, {"id": other, "error": "not_a_dropped_copy"}]


def test_dropped_copies_bucket_respects_the_cap(provider, served, state_db):
    app, _ = served
    cap = core.BUCKET_CAP
    with app.lock:
        x, *copies = _seed(app.conn, "Original X.", *[f"Copy {i} of X." for i in range(cap + 1)])
        (spare,) = _seed(app.conn, "Spare claim.")
        _confirm_copies(app.conn, x, *copies)
        _legacy_drop(app.conn, x)
        assert len(_ids(app.conn)) == cap + 1
    stamp = _call(provider, "review_claims", action="digest")["buckets"]["dropped_copies"]["stamp"]
    _turn(state_db, "ok")
    assert _call(provider, "review_claims", action="apply", bucket="dropped_copies", bucket_stamp=stamp,
                 approval_quote="ok")["error"] == "bucket_too_large"

    # Exactly the cap fits a fresh approval, but not one that already decided a claim.
    with app.lock:
        core.reject(app.conn, copies[-1], batch="hermes:tg1:redecided")  # the owner decides one again
        core.undo_batch(app.conn, "hermes:tg1:redecided")
        core.suggest(app.conn, spare, action="accept", by="assistant", suggested_kind="taste")
    stamp = _call(provider, "review_claims", action="digest")["buckets"]["dropped_copies"]["stamp"]
    reply = "yes please"
    _turn(state_db, reply)
    assert _call(provider, "review_claims", action="apply", ids=[spare], approval_quote=reply)["results"]
    with app.lock:
        before = app.conn.execute("SELECT count(*) FROM ledger_events").fetchone()[0]
    assert _call(provider, "review_claims", action="apply", bucket="dropped_copies", bucket_stamp=stamp,
                 approval_quote=reply)["error"] == "approval_cap_reached"
    with app.lock:
        assert app.conn.execute("SELECT count(*) FROM ledger_events").fetchone()[0] == before
    _turn(state_db, "ok")
    out = _call(provider, "review_claims", action="apply", bucket="dropped_copies", bucket_stamp=stamp,
                approval_quote="ok")
    assert len(out["results"]) == cap and out["errors"] == []
    with app.lock:
        assert _ids(app.conn) == []


def test_dropped_copies_clear_by_page(provider, served, state_db):
    app, _ = served
    with app.lock:
        x, c1, c2, other = _seed(app.conn, "Original X.", "Copy one of X.", "Copy two of X.", "Other.")
        _confirm_copies(app.conn, x, c1, c2)
        _legacy_drop(app.conn, x)
    _turn(state_db, "ok")
    out = _call(provider, "review_claims", action="apply", bucket="dropped_copies", ids=[c1, other],
                approval_quote="ok")
    assert out["bucket"] == "dropped_copies"
    assert [(r["id"], r["review_status"]) for r in out["results"]] == [(c1, "rejected")]
    assert out["errors"] == [{"id": other, "error": "not_a_dropped_copy"}]
    with app.lock:
        assert _status(app.conn, c1, c2, other) == ["rejected", "pending", "pending"] and _ids(app.conn) == [c2]
    # Other buckets still take a stamp, never ids.
    assert _call(provider, "review_claims", action="apply", bucket="reject", ids=[c2],
                 approval_quote="ok")["error"] == "ids_or_bucket"
