"""Read-only memory views of the control sidecar: synthetic ledger, receipts and sidecar only."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading

import pytest

from quoth import sidecar
from quoth.control.api import PREFIX, ControlApp
from quoth.control.memory import MemoryViews, connect_ro
from quoth.episodes import Episodes
from quoth.ledger import core as ledger
from quoth.ledger.compile import publish_core

SENTINEL = "PRIVATE_SENTINEL_NOTE_TEXT"


def call(app, path, method="GET", body=None, token="synthetic-token"):
    data = b"" if body is None else json.dumps(body).encode()
    headers = {}

    def respond(status, fields):
        headers["status"] = int(status[:3])
        headers.update(dict(fields))
    env = {"PATH_INFO": path, "REQUEST_METHOD": method, "QUERY_STRING": "",
           "CONTENT_TYPE": "application/json", "CONTENT_LENGTH": str(len(data)),
           "wsgi.input": io.BytesIO(data)}
    if token is not None:
        env["HTTP_X_HERMES_SIDECAR_TOKEN"] = token
    result = app(env, respond)
    return headers, json.loads(b"".join(result) or b"null")


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
    db.execute("INSERT INTO sessions(id, source) VALUES ('tg1', 'telegram')")
    db.executemany("INSERT INTO messages(id, session_id, role, active, timestamp, content) VALUES (?,?,?,?,?,?)", [
        (1, "tg1", "user", 1, 1_760_000_000.0, "[Ari] I keep the kayak in the blue shed."),
        (2, "tg1", "assistant", 1, 1_760_000_010.0,
         "The kayak paddles fit on the left rack of the blue shed, next to the life vests."),
    ])
    db.commit()
    db.close()
    return path


@pytest.fixture
def ledger_db(tmp_path):
    """Synthetic ledger: accepted, pending with presort, superseded chain, rejected, imported."""
    path = tmp_path / "ledger.db"
    conn = ledger.open_ledger(path)
    first = ledger.propose(conn, text="Ari keeps the kayak in the blue shed.", kind="environment",
                           origin="assistant_proposal", proposer="assistant", reason="said so")["claim"]["id"]
    ledger.accept(conn, first, note="approved via hermes:tg1:1")
    second = ledger.propose(conn, text="Ari keeps the kayak in the green shed.", kind="environment",
                            origin="assistant_proposal", proposer="assistant", supersedes=first)["claim"]["id"]
    ledger.accept(conn, second)
    tea = ledger.propose(conn, text="Ari prefers mint tea after lunch.", kind="preference",
                         origin="assistant_proposal", proposer="assistant")["claim"]["id"]
    ledger.suggest(conn, tea, action="accept", by="assistant", note="matches chat")
    dup = ledger.propose(conn, text="The kayak lives in the green shed.", kind="environment",
                         origin="assistant_proposal", proposer="assistant")["claim"]["id"]
    ledger.suggest(conn, dup, action="duplicate", by="assistant", duplicate_of=second)
    imported = ledger.propose(conn, text="Likes long walks by the canal.", kind="unclassified",
                            origin="imported", proposer="builtin_memory")["claim"]["id"]
    gone = ledger.propose(conn, text="Ari owns a red bicycle.", kind="person",
                          origin="assistant_proposal", proposer="assistant")["claim"]["id"]
    ledger.reject(conn, gone, note="not true")
    yield path, conn, {"first": first, "second": second, "tea": tea, "dup": dup, "imported": imported,
                       "gone": gone}
    conn.close()


@pytest.fixture
def control(tmp_path, ledger_db):
    path, conn, ids = ledger_db
    token = tmp_path / "quoth.token"
    token.write_text("synthetic-token")
    core_dir = tmp_path / "core"
    publish_core(conn, core_dir)
    receipts = tmp_path / "nightly" / "receipts"
    receipts.mkdir(parents=True)
    memory = MemoryViews(ledger_path=path, core_dir=core_dir, nightly_receipts=receipts)
    # The lab snapshot is missing on purpose: memory views must not depend on it.
    app = ControlApp(tmp_path / "missing.db", "missing", "0" * 64, token, memory=memory)
    assert not app.ready
    return app, token, ids, receipts


ROUTES = [("POST", "/memory/claims", {}), ("GET", "/memory/claims/1", None), ("GET", "/memory/queue", None),
          ("GET", "/memory/core", None), ("GET", "/memory/core/versions/1", None),
          ("POST", "/memory/recall", {"text": "kayak"}), ("POST", "/memory/episodes", {"query": "kayak"}),
          ("GET", "/memory/sidecar", None), ("GET", "/memory/refresh", None), ("GET", "/memory/sources", None)]


def test_every_memory_route_requires_the_token(control):
    app, token, _, _ = control
    for method, route, body in ROUTES:
        for supplied in (None, "forged", ""):
            headers, out = call(app, PREFIX + route, method, body, token=supplied)
            assert headers["status"] == 401, route
            assert headers["Cache-Control"] == "no-store"
            assert out == {"error": {"code": "unauthorized", "retryable": False}}
    token.unlink()
    for method, route, body in ROUTES:
        assert call(app, PREFIX + route, method, body)[0]["status"] == 503
    token.write_text("synthetic-token")
    # Fixed route table: wrong method, unknown route, traversal and query strings are 404.
    for method, route in (("GET", "/memory/claims"), ("POST", "/memory/queue"), ("DELETE", "/memory/claims/1"),
                          ("POST", "/memory/claims/1"), ("GET", "/memory/claims/0"), ("GET", "/memory/../status"),
                          ("GET", "/memory/claims/1/accept"), ("POST", "/memory/accept")):
        assert call(app, PREFIX + route, method, {} if method == "POST" else None)[0]["status"] in (404, 400)
    env_query = call(app, PREFIX + "/memory/queue")
    assert env_query[0]["status"] == 200


def test_no_write_routes_exist(control):
    app, _, ids, _ = control
    for action in ("accept", "reject", "edit", "forget", "apply", "suggest", "undo_auto", "review"):
        for method in ("POST", "DELETE", "PUT"):
            for route in (f"/memory/{action}", f"/memory/claims/{ids['tea']}/{action}"):
                assert call(app, PREFIX + route, method, {})[0]["status"] == 404


def test_claims_table_filters_search_and_paging(control):
    app, _, ids, _ = control
    _, out = call(app, PREFIX + "/memory/claims", "POST", {})
    assert out["total"] == 6 and [c["id"] for c in out["items"]] == sorted(ids.values(), reverse=True)
    assert out["ledger_totals"] == {"accepted": 2, "pending": 3, "rejected": 1}
    row = next(c for c in out["items"] if c["id"] == ids["tea"])
    assert row["suggestion"] == "accept" and row["authority"] == "assistant" and "evidence_quote" not in row
    by = lambda **f: {c["id"] for c in call(app, PREFIX + "/memory/claims", "POST", f)[1]["items"]}
    assert by(status="pending") == {ids["tea"], ids["dup"], ids["imported"]}
    assert by(status="accepted") == {ids["first"], ids["second"]}  # first is accepted but superseded
    assert by(status="current") == {ids["second"], ids["tea"], ids["dup"], ids["imported"]}
    assert by(status="rejected") == {ids["gone"]}
    assert by(authority="imported") == {ids["imported"]}
    assert by(authority="owner") == set()
    assert by(kind="preference") == {ids["tea"]}
    assert by(q="kayak shed") == {ids["first"], ids["second"], ids["dup"]}
    assert by(q="green shed", status="pending") == {ids["dup"]}
    _, page = call(app, PREFIX + "/memory/claims", "POST", {"limit": 2, "offset": 2})
    assert page["total"] == 6 and len(page["items"]) == 2
    for bad in ({"status": "all"}, {"kind": "x"}, {"authority": "everyone"}, {"limit": 0}, {"limit": 101},
                {"offset": -1}, {"q": "x" * 201}, {"limit": "5"}, {"extra": 1}):
        assert call(app, PREFIX + "/memory/claims", "POST", bad)[0]["status"] == 400, bad


def test_claim_detail_has_chain_suggestion_and_full_history(control):
    app, _, ids, _ = control
    headers, out = call(app, PREFIX + f"/memory/claims/{ids['first']}")
    assert headers["status"] == 200
    c = out["claim"]
    assert c["origin"] == "assistant_proposal" and c["quote_verified"] == 0
    assert c["superseded_by"] == ids["second"] and c["valid_to"] and c["review_note"].startswith("approved via")
    assert [e["action"] for e in out["events"]] == ["proposed", "accepted", "superseded"]
    assert out["events"][0]["detail"]["kind"] == "environment"
    assert out["events"][2]["detail"] == {"by": ids["second"]}
    assert [(x["id"], x.get("self", False)) for x in out["chain"]] == [(ids["first"], True), (ids["second"], False)]
    second = call(app, PREFIX + f"/memory/claims/{ids['second']}")[1]
    assert [x["id"] for x in second["chain"]] == [ids["first"], ids["second"]]
    tip = call(app, PREFIX + f"/memory/claims/{ids['dup']}")[1]["suggestion"]
    assert tip["action"] == "duplicate" and tip["duplicate_of"] == ids["second"]
    assert call(app, PREFIX + "/memory/claims/999")[0]["status"] == 404


def test_queue_grouped_by_presort_with_counts(control):
    app, _, ids, _ = control
    _, out = call(app, PREFIX + "/memory/queue")
    assert out["pending"] == 3
    assert out["counts"] == {"accept": 1, "duplicate": 1, "covered": 0, "reject": 0, "stale": 0, "keep": 0,
                             "none": 1}
    assert [c["id"] for c in out["groups"]["accept"]] == [ids["tea"]]
    assert [c["id"] for c in out["groups"]["none"]] == [ids["imported"]]
    assert out["groups"]["duplicate"][0]["suggestion"]["duplicate_of"] == ids["second"]
    assert not out["truncated"]
    # The digest plan and topics, as the assistant walks them in chat; read-only, no stamps leak.
    assert out["plan"] == {"cleanup": 1, "to_read": 2, "unsorted": 1, "round_size": 10, "rounds": 1}
    assert [t["topic"] for t in out["topics"]] == ["Other"] and out["topics"][0]["to_read"] == 2
    assert out["buckets"] == {"duplicate": 1, "covered": 0, "stale": 0, "reject": 0}
    # Positions follow digest numbering: accept, keep, none first, then the clean-up buckets.
    position = {c["id"]: c["position"] for g in out["groups"].values() for c in g}
    assert position == {ids["tea"]: 1, ids["imported"]: 2, ids["dup"]: 3}
    target = out["groups"]["duplicate"][0]["duplicate_target"]
    assert target == {"id": ids["second"], "text": "Ari keeps the kayak in the green shed.",
                      "review_status": "accepted", "kept": True, "kept_id": ids["second"]}


def test_queue_positions_match_the_digest_round(control, ledger_db):
    app, _, _, _ = control
    _, conn, _ = ledger_db
    _, out = call(app, PREFIX + "/memory/queue")
    group = ledger.digest(conn, topic="Other", limit=50)["group"]["items"]
    position = {c["id"]: c["position"] for g in out["groups"].values() for c in g}
    assert [(i["n"], i["id"]) for i in group] == sorted((n, i) for i, n in position.items())


def test_core_published_live_and_versions(control, ledger_db):
    app, _, ids, _ = control
    _, out = call(app, PREFIX + "/memory/core")
    assert out["source"] == "published" and out["file"].startswith("core-")
    assert "green shed" in out["text"] and out["live_differs"] is False
    assert len(out["versions"]) == 1 and out["versions"][0]["claims"] == 1
    version = call(app, PREFIX + f"/memory/core/versions/{out['versions'][0]['id']}")[1]
    assert version["text"] == out["text"]
    assert call(app, PREFIX + "/memory/core/versions/99")[0]["status"] == 404
    # A newer acceptance makes the published core stale; the view says so, without publishing.
    _, conn, _ = ledger_db
    ledger.accept(conn, ids["tea"])
    stale = call(app, PREFIX + "/memory/core")[1]
    assert stale["live_differs"] is True and len(stale["versions"]) == 1
    app.memory.core_dir = None
    live = call(app, PREFIX + "/memory/core")[1]
    assert live["source"] == "live" and "mint tea" in live["text"]


def test_ledger_is_opened_read_only_and_never_blocks_the_writer(control, ledger_db):
    app, _, ids, _ = control
    path, conn, _ = ledger_db
    reader = connect_ro(path)
    try:
        assert reader.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            reader.execute("UPDATE claims SET use_count = 99")
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM claims").fetchone()
        # The live sidecar keeps writing while a WebUI read transaction is open (WAL).
        ledger.propose(conn, text="Synthetic write during a read.", kind="pointer", origin="assistant_proposal",
                       proposer="assistant")
        reader.execute("COMMIT")
    finally:
        reader.close()
    uri_only = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            uri_only.execute("INSERT INTO ledger_events(action, actor) VALUES ('x', 'y')")
    finally:
        uri_only.close()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = (hashlib.sha256(path.read_bytes()).hexdigest(),
              conn.execute("SELECT max(id) FROM ledger_events").fetchone()[0],
              conn.execute("SELECT sum(use_count) FROM claims").fetchone()[0], conn.execute("PRAGMA data_version").fetchone()[0])
    for method, route, body in ROUTES:
        call(app, PREFIX + route, method, body)
    call(app, PREFIX + f"/memory/claims/{ids['dup']}")
    call(app, PREFIX + "/memory/claims", "POST", {"q": "kayak", "status": "current"})
    after = (hashlib.sha256(path.read_bytes()).hexdigest(),
             conn.execute("SELECT max(id) FROM ledger_events").fetchone()[0],
             conn.execute("SELECT sum(use_count) FROM claims").fetchone()[0], conn.execute("PRAGMA data_version").fetchone()[0])
    assert before == after


def test_unconfigured_sources_say_so(tmp_path):
    token = tmp_path / "t"
    token.write_text("synthetic-token")
    app = ControlApp(tmp_path / "missing.db", "missing", "0" * 64, token)
    expected = {"/memory/claims": "ledger_unconfigured", "/memory/queue": "ledger_unconfigured",
                "/memory/core": "core_unconfigured", "/memory/recall": "sidecar_unconfigured",
                "/memory/episodes": "sidecar_unconfigured", "/memory/refresh": "receipts_unconfigured"}
    for method, route, body in ROUTES:
        if route in expected:
            headers, out = call(app, PREFIX + route, method, body)
            assert headers["status"] == 503 and out["error"]["code"] == expected[route], route
    assert call(app, PREFIX + "/memory/sources")[1] == {"ledger": False, "ledger_via": None, "core_dir": False,
                                                         "sidecar": False, "nightly_receipts": False,
                                                         "heartbeat": False}
    app.memory.ledger_path = tmp_path / "absent.db"
    assert call(app, PREFIX + "/memory/queue")[1]["error"]["code"] == "ledger_unavailable"
    assert not (tmp_path / "absent.db").exists()  # mode=ro never creates a file
    app.memory.sidecar_socket = tmp_path / "absent.sock"
    assert call(app, PREFIX + "/memory/recall", "POST", {"text": "kayak"})[1]["error"]["code"] == "sidecar_unavailable"


@pytest.fixture
def quoth_socket(tmp_path, ledger_db, state_db):
    path, _, _ = ledger_db
    episodes = Episodes(tmp_path / "episodes.db", model="hash-ngram")
    episodes.ingest(state_db)
    episodes.embed(100)
    app = sidecar.Quoth(ledger_path=path, state_db=state_db, model="hash-ngram", episodes=episodes,
                            episode_min_score=0.0, episodes_prefetch=True)
    short = Path(tempfile.mkdtemp(prefix="mn"))
    server = sidecar.SidecarServer(short / "s.sock", app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield app, server.socket_path
    server.shutdown()
    server.server_close()
    app.close()
    shutil.rmtree(short, ignore_errors=True)


def test_recall_is_the_sidecars_own_dry_run(control, quoth_socket, ledger_db):
    app, _, ids, _ = control
    mn, socket_path = quoth_socket
    _, conn, _ = ledger_db
    app.memory.sidecar_socket = socket_path
    used = conn.execute("SELECT sum(use_count) FROM claims").fetchone()[0]
    headers, out = call(app, PREFIX + "/memory/recall", "POST", {"text": "where is the kayak shed"})
    assert headers["status"] == 200
    assert [c["id"] for c in out["claims"]] == [ids["second"]]  # accepted + current only
    assert out["claims"][0]["match"] == "words" and out["claims"][0]["cosine"] is None
    assert out["evidence_status"] == "disabled" and out["evidence"] == []
    assert out["episodes_status"] == "ok" and out["episodes"][0]["locator"] == "hermes:tg1:2"
    assert "score" in out["episodes"][0]
    # Exactly the text the assistant's prefetch would inject for the same turn.
    direct = mn.prefetch({"query": "where is the kayak shed", "dry_run": True})
    assert out["text"] == direct["text"] and "#%d" % ids["second"] in out["text"]
    assert conn.execute("SELECT sum(use_count) FROM claims").fetchone()[0] == used  # dry run: not marked used
    assert call(app, PREFIX + "/memory/recall", "POST", {"text": ""})[0]["status"] == 400
    assert call(app, PREFIX + "/memory/recall", "POST", {"text": "x" * 2049})[0]["status"] == 400
    _, found = call(app, PREFIX + "/memory/episodes", "POST", {"query": "kayak paddles", "k": 3})
    assert found["hits"][0]["locator"] == "hermes:tg1:2" and "left rack" in found["hits"][0]["text"]
    assert "Not the owner's" in found["answer_use"]
    assert call(app, PREFIX + "/memory/episodes", "POST", {"query": "kayak", "k": 21})[0]["status"] == 400
    health = call(app, PREFIX + "/memory/sidecar")[1]
    assert health["ledger"]["pending"] == 3 and health["episodes"]["messages"] == 1


def test_recall_marks_cosine_matches(control, quoth_socket):
    app, _, ids, _ = control
    mn, socket_path = quoth_socket
    app.memory.sidecar_socket = socket_path

    def fake_prefetch(body):
        assert body == {"query": "kayak", "dry_run": True}
        return {"text": "<quoth-recall>…</quoth-recall>", "claims": 1, "evidence": 1,
                "evidence_status": "ok", "detail": {
                    "claims": [{"id": 7, "kind": "environment", "authority": "assistant", "text": "t", "cosine": 0.71}],
                    "evidence": [{"locator": "PROJECTS/Garden.md:1-3", "note_date": "2026-01-02"}]}}
    mn.prefetch = fake_prefetch
    out = call(app, PREFIX + "/memory/recall", "POST", {"text": "kayak"})[1]
    assert out["claims"][0]["match"] == "cosine" and out["claims"][0]["cosine"] == 0.71
    assert out["evidence"] == [{"locator": "PROJECTS/Garden.md:1-3", "note_date": "2026-01-02"}]
    assert out["episodes"] == [] and out["episodes_status"] == "disabled"


def test_sidecar_calls_are_single_flight(control, quoth_socket):
    app, _, _, _ = control
    _, socket_path = quoth_socket
    app.memory.sidecar_socket = socket_path
    assert app.memory.sidecar_slot.acquire(blocking=False)
    try:
        headers, out = call(app, PREFIX + "/memory/recall", "POST", {"text": "kayak"})
        assert headers["status"] == 429 and out["error"] == {"code": "busy", "retryable": True}
    finally:
        app.memory.sidecar_slot.release()


def test_refresh_history_is_an_allowlist(control):
    app, _, _, receipts = control
    good = {"schema": "quoth-nightly/v1", "receipt_id": "20261026T020000Z-aaaaaaaaaaaa", "outcome": "SWITCH",
            "gate_mode": "content", "reasons": [], "seconds": {"build": 120.5, "eval": 30.25, "self_check": 2},
            "gold_drift": {"renamed": 2, "stale": 1, "ids": ["q-1"]},
            "recorded_at": "2026-10-26T02:05:00Z", "old": {"db": f"/x/{SENTINEL}.db"},
            "candidate": f"/x/{SENTINEL}.db", "lost_gold_ids": [SENTINEL],
            "excused_lost_gold": {"private": [{"id": SENTINEL, "reason": "gold_note_changed"}]},
            "serving_quality": {"status": "ok", "note": SENTINEL}}
    held = {"schema": "quoth-nightly/v1", "receipt_id": "20261027T020000Z-bbbbbbbbbbbb", "outcome": "HOLD",
            "gate_mode": "code", "reasons": ["private_hit@10_regressed", f"{SENTINEL} with spaces"],
            "seconds": {"build": 10, "bad": "x"}, "recorded_at": "2026-10-27T02:05:00Z"}
    (receipts / "20261026T020000Z-aaaaaaaaaaaa.json").write_text(json.dumps(good))
    (receipts / "20261027T020000Z-bbbbbbbbbbbb.json").write_text(json.dumps(held))
    (receipts / "quality-20261027.json").write_text(json.dumps({"schema": "other", "text": SENTINEL}))
    (receipts / "broken.json").write_text("{not json")
    outside = receipts.parent / "outside.json"
    outside.write_text(json.dumps({**good, "receipt_id": "outside"}))
    os.symlink(outside, receipts / "zz-link.json")
    (receipts.parent / "heartbeat.json").write_text(json.dumps({
        "schema": "quoth-heartbeat/v1", "checked_at": "2026-10-27T02:06:00Z", "status": "red",
        "reasons": ["last_run_hold"], "max_fresh_age_hours": 36.0, "max_run_age_hours": 26.0,
        "fresh_as_of": "2026-10-26T02:05:00Z", "fresh_age_hours": 24.0,
        "serving": {"db": f"/x/{SENTINEL}.db", "frozen_at": "2026-10-26T02:00:00Z", "age_hours": 24.1},
        "last_run": {"receipt_id": held["receipt_id"], "outcome": "HOLD", "reasons": ["x"],
                     "recorded_at": held["recorded_at"], "age_hours": 0.02}}))
    headers, out = call(app, PREFIX + "/memory/refresh")
    assert headers["status"] == 200
    assert SENTINEL not in json.dumps(out)
    assert [r["receipt_id"] for r in out["receipts"]] == [held["receipt_id"], good["receipt_id"]]
    newest, older = out["receipts"]
    assert newest["outcome"] == "HOLD" and newest["gate_mode"] == "code"
    assert newest["reasons"] == ["private_hit@10_regressed"] and newest["seconds"] == {"build": 10}
    assert older["gold_drift"] == {"renamed": 2, "stale": 1} and older["total_seconds"] == 152.8
    hb = out["heartbeat"]
    assert hb["status"] == "red" and hb["reasons"] == ["last_run_hold"]
    assert hb["serving"] == {"frozen_at": "2026-10-26T02:00:00Z", "age_hours": 24.1}
    assert hb["last_run"]["outcome"] == "HOLD"
    (receipts.parent / "heartbeat.json").write_text(json.dumps({"schema": "nope"}))
    assert call(app, PREFIX + "/memory/refresh")[1]["heartbeat"] == {"state": "invalid"}
    (receipts.parent / "heartbeat.json").unlink()
    assert call(app, PREFIX + "/memory/refresh")[1]["heartbeat"] == {"state": "unavailable"}


# ------------------------------------------------------------------ socket mode (deployed shape)

@pytest.fixture
def socket_mode(tmp_path, control, ledger_db, state_db):
    """Control process with no ledger file: the sidecar serves the ledger and core views."""
    app, token, ids, receipts = control
    path, _, _ = ledger_db
    mn = sidecar.Quoth(ledger_path=path, state_db=state_db, model="hash-ngram",
                           core_dir=tmp_path / "core")
    short = Path(tempfile.mkdtemp(prefix="mn"))
    server = sidecar.SidecarServer(short / "s.sock", mn)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    remote = ControlApp(None, None, None, token, memory=MemoryViews(
        sidecar_socket=server.socket_path, nightly_receipts=receipts))
    yield app, remote, ids
    server.shutdown()
    server.server_close()
    mn.close()
    shutil.rmtree(short, ignore_errors=True)


def test_socket_mode_matches_direct_reads(socket_mode):
    direct, remote, ids = socket_mode
    assert remote.memory_only and remote.memory._remote()
    sources = call(remote, PREFIX + "/memory/sources")[1]
    assert sources["ledger"] is True and sources["ledger_via"] == "sidecar"
    for method, route, body in [("POST", "/memory/claims", {}),
                                ("POST", "/memory/claims", {"status": "pending", "q": "kayak"}),
                                ("GET", f"/memory/claims/{ids['second']}", None),
                                ("GET", "/memory/queue", None), ("GET", "/memory/core", None),
                                ("GET", "/memory/core/versions/1", None)]:
        want = call(direct, PREFIX + route, method, body)
        got = call(remote, PREFIX + route, method, body)
        assert got[0]["status"] == want[0]["status"] == 200, route
        assert got[1] == want[1], route
    # Errors keep their status and code across the socket.
    headers, out = call(remote, PREFIX + "/memory/claims/999")
    assert headers["status"] == 404 and out["error"]["code"] == "claim_unknown"
    headers, out = call(remote, PREFIX + "/memory/claims", "POST", {"status": "bogus"})
    assert headers["status"] == 400 and out["error"]["code"] == "invalid_filter"


def test_socket_mode_never_writes_the_ledger(socket_mode, ledger_db):
    _, remote, _ = socket_mode
    _, conn, _ = ledger_db
    before = conn.execute("SELECT count(*), sum(use_count) FROM claims").fetchone()
    events = conn.execute("SELECT count(*) FROM ledger_events").fetchone()[0]
    for route in ("/memory/queue", "/memory/core"):
        call(remote, PREFIX + route)
    call(remote, PREFIX + "/memory/claims", "POST", {})
    assert tuple(conn.execute("SELECT count(*), sum(use_count) FROM claims").fetchone()) == tuple(before)
    assert conn.execute("SELECT count(*) FROM ledger_events").fetchone()[0] == events


def test_sidecar_ledger_view_route_refuses_anything_else(socket_mode):
    from quoth.control.memory import MemoryViewError
    _, remote, _ = socket_mode
    for body in ({"view": "apply"}, {"view": "claim", "id": "1"}, {"view": "claims", "filters": []}):
        with pytest.raises(MemoryViewError) as err:
            remote.memory._sidecar("POST", "/v1/ledger-view", body, single_flight=False)
        assert err.value.status == 400


def test_memory_only_control_is_healthy_and_lab_is_off(tmp_path):
    token = tmp_path / "t"
    token.write_text("synthetic-token")
    app = ControlApp(None, None, None, token, memory=MemoryViews())
    headers, out = call(app, "/health", token=None)
    assert headers["status"] == 200 and out == {"status": "ready", "lab": "disabled"}
    for route in ("/status", "/lab/operators", "/lab/pipelines/production"):
        headers, out = call(app, PREFIX + route)
        assert headers["status"] == 503 and out["error"]["code"] == "snapshot_unavailable"
    token.unlink()
    assert call(app, "/health", token=None)[0]["status"] == 503


def test_control_cli_lab_flags_go_together(tmp_path):
    from quoth.control.__main__ import main
    with pytest.raises(SystemExit):
        main(["--token-file", str(tmp_path / "t"), "--snapshot", str(tmp_path / "x.db")])
