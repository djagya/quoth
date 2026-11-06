"""The sidecar republishes the core after a review that changes accepted memory.

The assistant's gateway has no `quoth` command, so the sidecar, which holds the ledger and
the core directory, publishes the core itself. Synthetic claims only."""
from __future__ import annotations

from quoth import sidecar
from quoth.ledger import core
from quoth.ledger.compile import published_core
from test_guided_review import T0, assistant_replies, say, state_db  # noqa: F401 (fixtures)
from test_sidecar import _call, provider, served, snapshot_db  # noqa: F401 (fixtures)


def _taste(conn, text):
    return core.propose(conn, text=text, kind="taste", origin="assistant_proposal", proposer="assistant",
                        now=T0)["claim"]["id"]


def _turn(state_db, reply):
    assistant_replies(state_db)
    return say(state_db, reply)


def _published(app) -> str:
    return published_core(app.core_dir)["text"]


def _sha(app) -> str:
    return (app.core_dir / "core.md").resolve().name.removeprefix("core-").removesuffix(".md")


def test_each_memory_change_republishes_the_core(provider, served, state_db, tmp_path):
    app, _ = served
    app.core_dir = tmp_path / "core"
    with app.lock:
        a, b, c, d = (_taste(app.conn, t) for t in ("Likes green tea.", "Reads at night.",
                                                     "Walks every morning.", "Keeps a garden."))
    assert published_core(app.core_dir) is None

    _turn(state_db, "ok")
    out = _call(provider, "review_claims", action="accept", ids=[a, b], approval_quote="ok")
    assert out["core"]["claims"] == 2 and out["core"]["published"].startswith(_sha(app))
    assert "- Likes green tea. [inferred]" in _published(app)

    _turn(state_db, "drop 1")
    dropped = _call(provider, "review_claims", action="reject", ids=[c], approval_quote="drop 1")
    assert "core" not in dropped  # a reject never changes accepted memory

    _turn(state_db, "1: Likes green tea, unsweetened.")
    edited = _call(provider, "review_claims", action="edit", id=a, text="Likes green tea, unsweetened.",
                   approval_quote="1: Likes green tea, unsweetened.")
    assert edited["core"]["claims"] == 2 and "unsweetened" in _published(app)

    _turn(state_db, "please forget that I read at night")
    forgot = _call(provider, "review_claims", action="forget", id=b, reason="no longer true",
                   approval_quote="please forget that I read at night")
    assert forgot["core"]["claims"] == 1 and "Reads at night." not in _published(app)

    _call(provider, "review_claims", action="suggest", id=d, suggestion="accept")
    _turn(state_db, "ok")
    applied = _call(provider, "review_claims", action="apply", ids=[d], approval_quote="ok")
    assert applied["core"]["claims"] == 2 and "Keeps a garden." in _published(app)
    _turn(state_db, "undo")
    undone = _call(provider, "review_claims", action="undo", approval_quote="undo")
    assert undone["core_stale"] is True and undone["core"]["claims"] == 1
    assert "Keeps a garden." not in _published(app)
    summary = _call(provider, "review_claims", action="summary")  # reading publishes nothing
    assert "core" not in summary
    assert app.health()["core_publish"]["published"] == undone["core"]["published"]


def test_a_publish_failure_never_fails_the_decision(provider, served, state_db, tmp_path, monkeypatch):
    app, _ = served
    app.core_dir = tmp_path / "core"
    with app.lock:
        a = _taste(app.conn, "Likes green tea.")

    def broken(*_args, **_kw):
        raise PermissionError("read-only core directory")

    monkeypatch.setattr(sidecar, "publish_core", broken)
    _turn(state_db, "ok")
    out = _call(provider, "review_claims", action="accept", ids=[a], approval_quote="ok")
    assert out["errors"] == [] and out["results"][0]["review_status"] == "accepted"
    assert out["core"] == {"error": "publish_failed", "reason": "PermissionError"}
    with app.lock:
        assert core.get(app.conn, a)["review_status"] == "accepted"
    assert app.health()["core_publish"]["error"] == "publish_failed"
    assert published_core(app.core_dir) is None


def test_without_a_core_dir_nothing_is_published(provider, served, state_db):
    app, _ = served
    assert app.core_dir is None and "core_publish" not in app.health()
    with app.lock:
        a = _taste(app.conn, "Likes green tea.")
    _turn(state_db, "ok")
    out = _call(provider, "review_claims", action="accept", ids=[a], approval_quote="ok")
    assert out["results"][0]["review_status"] == "accepted" and "core" not in out
    assert "- Likes green tea. [inferred]" in app.core()["text"]  # /v1/core compiles live
