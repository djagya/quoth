"""Health says when the published core no longer matches the ledger. Synthetic claims only."""
from __future__ import annotations

import json

from quoth.ledger import cli, core
from quoth.ledger.compile import compile_core, core_freshness, publish_core
from test_guided_review import T0, assistant_replies, ledger, say, state_db  # noqa: F401 (fixtures)
from test_sidecar import _call, provider, served, snapshot_db  # noqa: F401 (fixtures)


def _accepted(conn, text, kind="taste"):
    cid = core.propose(conn, text=text, kind=kind, origin="assistant_proposal", proposer="assistant",
                       now=T0)["claim"]["id"]
    core.accept(conn, cid)
    return cid


def _core(conn, core_dir):
    return core.health(conn, core_dir=core_dir)


def test_core_goes_red_until_published_and_after_each_change(ledger, tmp_path):
    core_dir = tmp_path / "core"
    assert "core" not in core.health(ledger)  # no core dir: no core check
    empty = _core(ledger, core_dir)
    assert empty["status"] == "green" and empty["core"] == {
        "status": "green", "reason": None, "published_sha": None, "missing": 0, "extra": 0,
        "compiled_at": None}

    a = _accepted(ledger, "Likes green tea.")
    unpublished = _core(ledger, core_dir)
    assert unpublished["status"] == "red" and unpublished["reasons"] == ["core_unpublished"]
    assert unpublished["core"]["status"] == "red" and unpublished["core"]["missing"] == 1

    manifest = publish_core(ledger, core_dir)
    fresh = _core(ledger, core_dir)
    assert fresh["status"] == "green" and fresh["reasons"] == []
    assert fresh["core"] == {"status": "green", "reason": None, "published_sha": manifest["sha256"],
                             "missing": 0, "extra": 0, "compiled_at": manifest["compiled_at"]}

    b = _accepted(ledger, "Reads at night.")  # accepted without publishing
    stale = _core(ledger, core_dir)
    assert stale["status"] == "red" and stale["reasons"] == ["core_stale"]
    assert (stale["core"]["missing"], stale["core"]["extra"]) == (1, 0)
    assert stale["core"]["published_sha"] == manifest["sha256"]
    assert "Reads at night" not in json.dumps(stale)  # counts only, never claim text
    publish_core(ledger, core_dir)
    assert _core(ledger, core_dir)["core"]["status"] == "green"

    core.forget(ledger, a, "no longer true")  # the published core still carries it
    gone = _core(ledger, core_dir)["core"]
    assert (gone["status"], gone["missing"], gone["extra"]) == ("red", 0, 1)
    publish_core(ledger, core_dir)

    edited = core.edit(ledger, b, text="Reads every night.")  # one claim out, its rewrite in
    swapped = _core(ledger, core_dir)["core"]
    assert (swapped["status"], swapped["missing"], swapped["extra"]) == ("red", 1, 1)
    publish_core(ledger, core_dir)
    assert _core(ledger, core_dir)["core"]["status"] == "green" and edited["id"] != b


def test_use_counts_reorder_the_core_without_making_it_stale(ledger, tmp_path):
    core_dir = tmp_path / "core"
    a = _accepted(ledger, "Likes green tea.")
    b = _accepted(ledger, "Reads at night.")
    manifest = publish_core(ledger, core_dir)
    core.mark_claims_used(ledger, [b])  # injected in a turn: b now sorts first
    reordered = compile_core(ledger)
    assert reordered["claim_ids"] == [b, a] and manifest["claim_ids"] == [a, b]
    assert _core(ledger, core_dir)["core"]["status"] == "green"


def test_budget_drops_count_as_known_to_the_published_core(ledger, tmp_path):
    core_dir = tmp_path / "core"
    first = _accepted(ledger, "Likes green tea.")
    second = _accepted(ledger, "Reads at night.")
    budget = 260  # room for one claim only
    manifest = publish_core(ledger, core_dir, budget)
    assert manifest["claim_ids"] == [first] and manifest["dropped_ids"] == [second]
    core.mark_claims_used(ledger, [second, second])  # now the second claim wins the slot
    assert compile_core(ledger, budget)["claim_ids"] == [second]
    assert core_freshness(ledger, core_dir)["status"] == "green"  # same claims, other order of use
    assert core_freshness(ledger, core_dir, budget)["status"] == "green"


def test_an_unreadable_manifest_is_red(ledger, tmp_path):
    core_dir = tmp_path / "core"
    core_dir.mkdir()
    (core_dir / "core.json").write_text("{not json")
    out = _core(ledger, core_dir)
    assert out["status"] == "red" and out["reasons"] == ["core_unreadable"]


def test_ledger_health_cli_exits_red_on_a_stale_core(tmp_path, capsys):
    path, core_dir = tmp_path / "ledger.db", tmp_path / "core"
    conn = core.open_ledger(path)
    try:
        _accepted(conn, "Likes green tea.")
    finally:
        conn.close()
    args = ["--ledger", str(path), "health", "--core-dir", str(core_dir)]
    assert cli.main(args) == 1
    assert json.loads(capsys.readouterr().out)["reasons"] == ["core_unpublished"]
    assert cli.main(["--ledger", str(path), "compile-core", "--publish", str(core_dir)]) == 0
    capsys.readouterr()
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)["core"]["status"] == "green"
    assert cli.main(["--ledger", str(path), "health"]) == 0  # without --core-dir, no core check
    assert "core" not in json.loads(capsys.readouterr().out)


def test_sidecar_health_reports_core_freshness(provider, served, state_db, tmp_path):
    app, _ = served
    assert "core" not in app.health()  # no --core-dir
    app.core_dir = tmp_path / "core"
    with app.lock:
        a = _accepted(app.conn, "Likes green tea.")  # accepted outside the review: nothing published
        b = core.propose(app.conn, text="Reads at night.", kind="taste", origin="assistant_proposal",
                         proposer="assistant")["claim"]["id"]  # young: the queue stays green
    red = app.health()
    assert red["status"] == "red" and red["ledger"]["reasons"] == ["core_unpublished"]
    assert red["core"] == {"status": "red", "published_sha": None, "missing": 1, "extra": 0,
                           "compiled_at": None}
    with app.lock:
        publish_core(app.conn, app.core_dir, app.core_budget)
    assert app.health()["status"] == "green" and app.health()["core"]["status"] == "green"

    with app.lock:
        core.forget(app.conn, a, "no longer true")
    stale = app.health()
    assert stale["status"] == "red" and stale["ledger"]["reasons"] == ["core_stale"]
    assert (stale["core"]["missing"], stale["core"]["extra"]) == (0, 1)

    # A review accept republishes the core, which clears it.
    assistant_replies(state_db)
    say(state_db, "ok")
    out = _call(provider, "review_claims", action="accept", ids=[b], approval_quote="ok")
    green = app.health()
    assert green["status"] == "green" and green["ledger"]["reasons"] == []
    assert green["core"]["published_sha"] == out["core"]["published"]
    assert (green["core"]["missing"], green["core"]["extra"]) == (0, 0)
