"""Episodic lanes: what the assistant said and what the owner wrote, from a synthetic Hermes state.db."""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import pytest

from quoth import episodes as ep
from quoth import sidecar
from quoth.cli import main as cli_main

TOMATO = ("I recommend watering the balcony tomatoes every second morning and adding basil "
          "between the pots; skip watering when it rains.")
BIKE = "For the bike commute, the river path is flatter and about ten minutes faster."


def _schema(db):
    db.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT,
            parent_session_id TEXT, profile_name TEXT, user_id TEXT);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL,
            active INTEGER NOT NULL, timestamp REAL NOT NULL, content TEXT, display_kind TEXT,
            platform_message_id TEXT, compacted INTEGER NOT NULL DEFAULT 0);
    """)


@pytest.fixture
def state_db(tmp_path):
    path = tmp_path / "state.db"
    db = sqlite3.connect(path)
    _schema(db)
    db.executemany("INSERT INTO sessions(id, source) VALUES (?, ?)",
                   [("tg1", "telegram"), ("tg2", "telegram"), ("cron1", "cron")])
    envelope = "\x00json:" + json.dumps([{"type": "text", "text": BIKE},
                                         {"type": "image_url", "image_url": "data:xx"}])
    rows = [
        (1, "tg1", "user", 1, 0, 1_760_000_000.0, "[Ari] how often should I water the tomatoes?"),
        (2, "tg1", "assistant", 1, 0, 1_760_000_010.0, TOMATO),
        (3, "tg1", "assistant", 1, 0, 1_760_000_020.0, ""),  # tool-call only
        (4, "tg1", "tool", 1, 0, 1_760_000_030.0, "tool output that is long enough"),
        (5, "tg2", "assistant", 0, 1, 1_760_100_000.0, envelope),  # compaction original
        (6, "tg2", "assistant", 0, 0, 1_760_100_010.0, "A retracted draft that was never shown."),
        (7, "cron1", "assistant", 1, 0, 1_760_100_020.0, "Cron summary about tomatoes and basil."),
        (8, "tg2", "assistant", 1, 0, 1_760_100_030.0,
         "Use the key api_key = AKIAIOSFODNN7EXAMPLE for the staging service, then rotate it."),
        (9, "tg2", "assistant", 1, 0, 1_760_100_040.0, "Ok."),
    ]
    db.executemany("INSERT INTO messages(id, session_id, role, active, compacted, timestamp, content)"
                   " VALUES (?,?,?,?,?,?,?)", rows)
    db.commit()
    db.close()
    return path


@pytest.fixture
def eps(tmp_path):
    e = ep.Episodes(tmp_path / "episodes.db", model="hash-ngram")
    yield e
    e.close()


def _add(state_db, rows):
    db = sqlite3.connect(state_db)
    db.executemany("INSERT INTO messages(id, session_id, role, active, compacted, timestamp, content)"
                   " VALUES (?,?,?,?,?,?,?)", rows)
    db.commit()
    db.close()


def test_ingest_keeps_only_shown_telegram_assistant_text(eps, state_db):
    out = eps.ingest(state_db)
    assert out["added"] == 3 and out["watermark"] == 9
    rows = eps.conn.execute("SELECT session_id, message_id, text, redactions FROM episode_messages"
                            " ORDER BY message_id").fetchall()
    assert [(r[0], r[1]) for r in rows] == [("tg1", 2), ("tg2", 5), ("tg2", 8)]
    assert rows[1][2] == BIKE  # transport envelope unwrapped, image part dropped
    assert "AKIAIOSFODNN7EXAMPLE" not in rows[2][2] and rows[2][3] == 1
    # The source DB is opened read-only: nothing written there.
    assert sqlite3.connect(state_db).execute("SELECT count(*) FROM messages").fetchone()[0] == 9

    again = eps.ingest(state_db)
    assert again["scanned"] == 0 and again["added"] == 0
    _add(state_db, [(10, "tg1", "assistant", 1, 0, 1_760_200_000.0, "A new recommendation: try oat milk.")])
    third = eps.ingest(state_db)
    assert third["scanned"] == 1 and third["added"] == 1 and third["previous_watermark"] == 9
    assert eps.stats()["watermarks"] == {str(Path(state_db).resolve()): 10}


def test_chunk_text_windows_cover_long_messages():
    words = " ".join(f"w{i}" for i in range(900))
    chunks = ep.chunk_text(words)
    assert len(chunks) > 2 and all(len(c) <= ep.CHUNK_CHARS for c in chunks)
    assert chunks[0].startswith("w0 ") and chunks[-1].endswith("w899")
    seen = set(" ".join(chunks).split())
    assert seen == set(words.split())  # every word lands in some chunk; no word is cut
    assert ep.chunk_text("short") == ["short"] and ep.chunk_text("  ") == []


def test_embedding_is_bounded_and_search_is_assistant_origin(eps, state_db):
    long = " ".join(["The plan for the garden shed needs a dry floor and good airflow."] * 60)
    _add(state_db, [(11, "tg1", "assistant", 1, 0, 1_760_300_000.0, long)])
    eps.ingest(state_db)
    total = eps.pending()
    assert total > 4
    assert eps.embed(limit=2) == 2 and eps.pending() == total - 2
    assert eps.embed(limit=1000) == total - 2 and eps.pending() == 0

    res = eps.search("water the balcony tomatoes", k=3)
    assert res["origin"] == "assistant" and "Not the owner's" in res["answer_use"]
    top = res["hits"][0]
    assert top["locator"] == "hermes:tg1:2" and top["origin"] == "assistant"
    assert top["authority"] == "none" and top["timestamp"].startswith("2025-10-09")
    assert top["text"] == TOMATO
    # one hit per message even though message 11 has many chunks
    shed = eps.search("garden shed dry floor airflow", k=5)["hits"]
    assert len([h for h in shed if h["message_id"] == 11]) == 1
    assert eps.search("water the balcony tomatoes", k=3, exclude_session="tg1")["hits"][0][
        "session_id"] == "tg2"
    assert eps.search("water the balcony tomatoes", k=3, min_score=0.999)["hits"] == []
    with pytest.raises(ValueError):
        eps.search("", k=3)
    with pytest.raises(ValueError):
        eps.search("x", k=0)


def test_matrix_cache_invalidates_on_new_rows(eps, state_db, tmp_path):
    eps.ingest(state_db)
    eps.embed(100)
    ids, _ = eps._load_matrix()
    assert eps._load_matrix()[0] is ids  # cached
    _add(state_db, [(12, "tg1", "assistant", 1, 0, 1_760_400_000.0, "Remember the dentist on Friday.")])
    eps.ingest(state_db)
    eps.embed(100)
    assert len(eps._load_matrix()[0]) == len(ids) + 1
    # A write from another process (the operator CLI) is seen through data_version.
    other = ep.Episodes(eps.path, model="hash-ngram")
    _add(state_db, [(13, "tg1", "assistant", 1, 0, 1_760_500_000.0, "Pack the tent for the weekend.")])
    other.ingest(state_db)
    other.embed(100)
    other.close()
    assert eps.search("pack the tent", k=1)["hits"][0]["message_id"] == 13


def test_migrations_are_idempotent_and_refuse_newer(tmp_path):
    path = tmp_path / "e.db"
    ep.open_episodes(path).close()
    conn = ep.open_episodes(path)
    conn.execute("INSERT INTO episodes_migrations(version, name) VALUES (99, 'future')")
    conn.close()
    with pytest.raises(RuntimeError, match="newer"):
        ep.open_episodes(path)


def test_tick_heartbeat_and_health(eps, state_db, tmp_path):
    assert ep.health(eps.stats(), 300)["reasons"] == ["never_ticked"]
    out = eps.tick(state_db, limit=1)
    assert out["error"] is None and out["embedded"] == 1
    assert ep.health(eps.stats(), 300)["status"] == "green"
    bad = eps.tick(tmp_path / "missing.db")
    assert bad["error"] == "state_db_missing"
    h = ep.health(eps.stats(), 300)
    assert h["status"] == "red" and h["reasons"] == ["error:state_db_missing"]
    eps.set_state("last_tick_at", "2020-01-01T00:00:00+00:00")
    eps.set_state("last_error", "")
    assert ep.health(eps.stats(), 300)["reasons"] == ["stale"]


def test_worker_ticks_in_background(eps, state_db):
    worker = ep.EpisodeWorker(eps, state_db, interval=60).start()
    try:
        for _ in range(200):
            if eps.stats()["embedded"] == 3:
                break
            time.sleep(0.02)
        assert eps.stats()["embedded"] == 3 and eps.stats()["last_tick_at"]
    finally:
        worker.stop()
    assert not worker.thread.is_alive()


def test_cli_ingest_search_stats_health(tmp_path, state_db, capsys):
    db = str(tmp_path / "cli.db")
    base = ["episodes", "--episodes", db, "--model", "hash-ngram"]
    assert cli_main(base + ["ingest", "--state-db", str(state_db), "--max-chunks", "2"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ingest"]["added"] == 3 and out["embedded"] == 2 and out["pending"] == 1
    assert cli_main(base + ["ingest", "--state-db", str(state_db)]) == 0
    assert json.loads(capsys.readouterr().out)["pending"] == 0
    assert cli_main(base + ["search", "balcony tomatoes", "-k", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["hits"][0]["locator"] == "hermes:tg1:2"
    assert cli_main(base + ["stats"]) == 0
    assert json.loads(capsys.readouterr().out)["messages"] == 3
    assert cli_main(base + ["health"]) == 1  # the CLI ingest is not the background tick
    assert json.loads(capsys.readouterr().out)["reasons"] == ["never_ticked"]


# ------------------------------------------------------------------ sidecar


@pytest.fixture
def app_eps(tmp_path, state_db):
    e = ep.Episodes(tmp_path / "episodes.db", model="hash-ngram")
    e.ingest(state_db)
    e.embed(100)
    app = sidecar.Quoth(ledger_path=tmp_path / "ledger.db", state_db=state_db, model="hash-ngram",
                            episodes=e, episode_min_score=0.3, episodes_prefetch=True)
    yield app
    app.close()


def test_episodes_route_and_unconfigured(app_eps, tmp_path):
    res = app_eps.episodes_search({"query": "balcony tomatoes", "k": 2})
    assert res["hits"][0]["locator"] == "hermes:tg1:2" and res["origin"] == "assistant"
    with pytest.raises(sidecar.SidecarError) as err:
        app_eps.episodes_search({"query": "", "k": 2})
    assert err.value.code == "invalid_episodes_request"
    plain = sidecar.Quoth(ledger_path=tmp_path / "l2.db")
    try:
        with pytest.raises(sidecar.SidecarError) as err:
            plain.episodes_search({"query": "x"})
        assert err.value.status == 503 and err.value.code == "episodes_unconfigured"
        assert plain.health()["episodes"] == {"status": "disabled"}
        assert "episodes" not in plain.prefetch({"query": "balcony tomatoes"})
    finally:
        plain.close()


def test_prefetch_adds_assistant_section_above_threshold_only(app_eps):
    out = app_eps.prefetch({"query": "how often to water the balcony tomatoes", "session_id": "tg9"})
    assert out["episodes_status"] == "ok" and 1 <= out["episodes"] <= sidecar.PREFETCH_EPISODES
    text = out["text"]
    assert text.startswith("<quoth-recall>") and text.endswith("</quoth-recall>")
    assert sidecar.EPISODES_HEADER in text and "hermes:tg1:2 (2025-10-09)" in text
    assert "Accepted memory" not in text  # the assistant's words are not claims
    # The current session's own messages are already in context: excluded.
    same = app_eps.prefetch({"query": "how often to water the balcony tomatoes", "session_id": "tg1"})
    assert "hermes:tg1:2" not in same["text"]
    app_eps.assistant_lane.min_score = 0.999
    none = app_eps.prefetch({"query": "how often to water the balcony tomatoes"})
    assert none["episodes"] == 0 and none["text"] == ""


def test_prefetch_episodes_inside_existing_recall_block(app_eps):
    app_eps.propose({"text": "Ari grows tomatoes on the balcony.", "kind": "environment",
                     "reason": "r", "quote": "how often should I water the tomatoes",
                     "confidence": 0.95, "session_id": "tg1"})
    text = app_eps.prefetch({"query": "balcony tomatoes water"})["text"]
    assert text.count("<quoth-recall>") == 1
    assert text.index("Accepted memory") < text.index(sidecar.EPISODES_HEADER)


class _SlowEpisodes:
    def __init__(self):
        self.release = threading.Event()

    def search(self, query, k, **kw):
        self.release.wait(10)
        return {"hits": [{"locator": "hermes:s:1", "timestamp": "2026-01-01T00:00:00+00:00",
                          "text": "slow words"}]}

    def stats(self):
        return {}

    def close(self):
        pass


def test_prefetch_never_waits_on_episodes_beyond_budget(tmp_path):
    slow = _SlowEpisodes()
    app = sidecar.Quoth(ledger_path=tmp_path / "l.db", episodes=slow, evidence_budget=0.2,
                            episodes_prefetch=True)
    try:
        t0 = time.monotonic()
        first = app.prefetch({"query": "anything"})
        assert time.monotonic() - t0 < 1.0
        assert first["episodes_status"] == "slow" and "slow words" not in first["text"]
        assert app.prefetch({"query": "again"})["episodes_status"] == "busy"
        slow.release.set()
        app.assistant_lane._future.result(5)
        assert "slow words" in app.prefetch({"query": "third"})["text"]
    finally:
        slow.release.set()
        app.close()


def test_plugin_episode_search_tool(app_eps, monkeypatch, tmp_path):
    from test_sidecar import REPO, _load_plugin
    short = Path(tempfile.mkdtemp(prefix="mn"))
    server = sidecar.SidecarServer(short / "s.sock", app_eps)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        plugin = _load_plugin(monkeypatch, REPO / "hermes_plugin" / "quoth")
        p = plugin.QuothMemoryProvider(socket_path=str(server.socket_path))
        p.initialize("tg2", hermes_home=str(tmp_path), platform="telegram", agent_context="primary")
        assert "episode_search" in [s["name"] for s in p.get_tool_schemas()]
        res = json.loads(p.handle_tool_call("episode_search", {"query": "balcony tomatoes", "k": 1}))
        assert res["ok"] and res["hits"][0]["locator"] == "hermes:tg1:2"
        assert res["hits"][0]["origin"] == "assistant"
        recall = p.prefetch("water the balcony tomatoes", session_id="tg2")
        assert sidecar.EPISODES_HEADER in recall and p.recall_status().count >= 1
        health = json.loads(json.dumps(p._client.get("/v1/health")))
        assert health["episodes"]["messages"] == 3
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(short, ignore_errors=True)


def test_sidecar_wiring_shares_query_embedder(tmp_path, state_db):
    assert sidecar._open_episodes(None, "hash-ngram", None) is None
    e = sidecar._open_episodes(str(tmp_path / "w.db"), "hash-ngram", None)
    try:
        e.ingest(state_db)
        e.embed(100)
        assert e.search("balcony tomatoes", k=1)["hits"][0]["locator"] == "hermes:tg1:2"
    finally:
        e.close()


def test_embeds_newest_first_and_respects_max_age(eps, state_db, monkeypatch):
    eps.ingest(state_db)
    total = eps.pending()
    assert eps.embed(1) == 1
    with eps.lock:
        first = eps.conn.execute(
            "SELECT m.message_id FROM episode_chunks c JOIN episode_messages m ON m.id = c.message_ref"
            " WHERE c.vector IS NOT NULL").fetchall()
    assert [r[0] for r in first] == [8]  # the newest reply, not the oldest
    # Only replies within the last day (relative to the fixture clock) are embedded.
    monkeypatch.setattr(ep.time, "time", lambda: 1_760_100_100.0)
    eps.max_age_days = 1
    assert eps.pending() == total - 1 - 1  # message 2 (a day older) is out of the window
    eps.embed(100)
    assert eps.pending() == 0
    eps.max_age_days = None
    assert eps.pending() == 1  # still ingested, embedded later if the window widens


def test_assistant_lane_is_tool_only_by_default(tmp_path, state_db):
    e = ep.Episodes(tmp_path / "episodes.db", model="hash-ngram")
    e.ingest(state_db)
    e.embed(100)
    app = sidecar.Quoth(ledger_path=tmp_path / "ledger.db", state_db=state_db, model="hash-ngram",
                            episodes=e, episode_min_score=0.0)
    try:
        out = app.prefetch({"query": "how often to water the balcony tomatoes"})
        assert out["episodes_status"] == "off" and sidecar.EPISODES_HEADER not in out["text"]
        assert app.episodes_search({"query": "balcony tomatoes", "k": 1})["hits"]
        assert app.health()["episodes"]["prefetch"] is False
    finally:
        app.close()


OWNER_ROWS = [
    (20, "tg1", "user", 1, 0, 1_760_200_000.0, "[Ari] The garden shed roof leaks near the north corner."),
    (21, "tg1", "user", 1, 0, 1_760_200_010.0,
     '[Replying to: "The assistant\'s long advice about tomatoes and watering"]\n\n[Ari] ok, thanks for that'),
    (22, "tg1", "user", 1, 0, 1_760_200_020.0, "[The user sent an image~ a photo of a shed]\n\n[Ari] look"),
    (23, "tg1", "user", 1, 0, 1_760_200_030.0, "[Ari] my aws key AKIAIOSFODNN7EXAMPLE stays in 1Password"),
    (24, "tg2", "user", 1, 0, 1_760_200_040.0, "[Ari] I want the shed roof fixed before winter."),
]


def test_owner_lane_keeps_only_the_owners_own_words(tmp_path, state_db):
    _add(state_db, OWNER_ROWS)
    d = ep.Episodes(tmp_path / "owner.db", model="hash-ngram", role="owner")
    d.ingest(state_db)
    with d.lock:
        texts = {r[0]: r[1] for r in d.conn.execute("SELECT message_id, text FROM episode_messages")}
    assert texts[20] == "The garden shed roof leaks near the north corner."
    assert texts[21] == "ok, thanks for that"  # the quoted assistant text is not Ari's
    assert 22 not in texts  # machine image caption: fail closed
    assert "AKIA" not in texts[23]
    assert all(not t.startswith("[Ari]") for t in texts.values())
    d.embed(100)
    hit = d.search("shed roof leak", k=1)["hits"][0]
    assert hit["origin"] == "owner" and hit["authority"] == "unreviewed"
    d.close()


def test_lane_db_refuses_the_other_role(tmp_path, state_db):
    a = ep.Episodes(tmp_path / "a.db", model="hash-ngram")
    a.ingest(state_db)
    a.close()
    with pytest.raises(RuntimeError, match="assistant lane"):
        ep.Episodes(tmp_path / "a.db", model="hash-ngram", role="owner")
    # A DB that holds messages but records no role is the assistant lane.
    unmarked = ep.Episodes(tmp_path / "a.db", model="hash-ngram")
    with unmarked.lock:
        unmarked.conn.execute("DELETE FROM episode_state WHERE key = 'role'")
    unmarked.close()
    with pytest.raises(RuntimeError):
        ep.Episodes(tmp_path / "a.db", model="hash-ngram", role="owner")


def test_prefetch_puts_owners_words_before_assistants(tmp_path, state_db):
    _add(state_db, OWNER_ROWS)
    assistant = ep.Episodes(tmp_path / "episodes.db", model="hash-ngram")
    owner = ep.Episodes(tmp_path / "owner.db", model="hash-ngram", role="owner")
    for lane in (assistant, owner):
        lane.ingest(state_db)
        lane.embed(100)
    app = sidecar.Quoth(ledger_path=tmp_path / "ledger.db", state_db=state_db, model="hash-ngram",
                            episodes=assistant, episode_min_score=0.0, episodes_prefetch=True,
                            owner_episodes=owner, owner_min_score=0.0)
    try:
        out = app.prefetch({"query": "garden shed roof dry floor", "session_id": "tg9", "dry_run": True})
        text = out["text"]
        assert out["owner_episodes_status"] == "ok" and out["owner_episodes"] >= 1
        assert text.index(sidecar.OWNER_HEADER) < text.index(sidecar.EPISODES_HEADER)
        assert out["detail"]["owner_episodes"][0]["locator"].startswith("hermes:")
        res = app.episodes_search({"query": "shed roof", "k": 1, "who": "owner"})
        assert res["origin"] == "owner" and res["hits"][0]["origin"] == "owner"
        with pytest.raises(sidecar.SidecarError):
            app.episodes_search({"query": "shed roof", "who": "someone"})
        health = app.health()
        assert health["owner_episodes"]["prefetch"] is True and "messages" in health["owner_episodes"]
        view = app.view({"args": "recall garden shed roof"})["text"]
        assert "You wrote earlier (" in view and "said earlier (" in view
    finally:
        app.close()


def test_compaction_copies_are_stored_once(tmp_path, state_db):
    # Hermes re-saves the protected tail at compaction: same text, new message ids.
    _add(state_db, [(30, "tg1", "assistant", 1, 0, 1_760_300_000.0, TOMATO),
                    (31, "tg2", "assistant", 1, 0, 1_760_300_010.0, TOMATO)])
    e = ep.Episodes(tmp_path / "e.db", model="hash-ngram")
    stats = e.ingest(state_db)
    assert stats["duplicates"] == 2
    with e.lock:
        assert e.conn.execute("SELECT count(*) FROM episode_messages WHERE text = ?", (TOMATO,)).fetchone()[0] == 1
    e.close()



def test_ingest_counts_injected_turns_and_keeps_running_totals(tmp_path, state_db):
    db = sqlite3.connect(state_db)
    db.executemany("INSERT INTO messages(id, session_id, role, active, compacted, timestamp, content, display_kind)"
                   " VALUES (?,?,?,?,?,?,?,?)",
                   [(40, "tg1", "user", 1, 0, 1_760_400_000.0, "A background task finished its run.",
                     "internal_notification"),
                    (41, "tg1", "user", 1, 0, 1_760_400_010.0, "[Ari] the shed roof needs new felt", None)])
    db.commit()
    db.close()
    e = ep.Episodes(tmp_path / "d.db", model="hash-ngram", role="owner")
    first = e.ingest(state_db)
    assert first["injected"] == 1 and first["added"] == 2
    _add(state_db, [(42, "tg2", "user", 1, 0, 1_760_400_020.0, "[Ari] the shed roof needs new felt")])
    second = e.ingest(state_db)
    assert second["duplicates"] == 1 and second["injected"] == 0
    totals = e.stats()["ingest_totals"]
    assert totals == {"added": 2, "skipped": first["skipped"], "duplicates": 1, "injected": 1}
    e.close()

def test_existing_copies_are_removed_on_open_keeping_the_embedded_one(tmp_path, state_db):
    e = ep.Episodes(tmp_path / "e.db", model="hash-ngram")
    e.ingest(state_db)
    e.embed(100)
    with e.lock:  # simulate a pre-dedupe DB holding an unembedded copy
        e.conn.execute("INSERT INTO episode_messages(session_id, message_id, ts, text, redactions)"
                       " VALUES ('tg9', 99, 1.0, ?, 0)", (TOMATO,))
        ref = e.conn.execute("SELECT max(id) FROM episode_messages").fetchone()[0]
        e.conn.execute("INSERT INTO episode_chunks(message_ref, seq, text) VALUES (?, 0, ?)", (ref, TOMATO))
    e.close()
    again = ep.Episodes(tmp_path / "e.db", model="hash-ngram")
    with again.lock:
        rows = again.conn.execute("SELECT session_id FROM episode_messages WHERE text = ?", (TOMATO,)).fetchall()
    assert [r[0] for r in rows] == ["tg1"] and again.pending() == 0
    again.close()


def test_background_embedding_yields_to_live_queries(tmp_path, state_db, monkeypatch):
    e = ep.Episodes(tmp_path / "e.db", model="hash-ngram")
    e.ingest(state_db)
    monkeypatch.setattr(ep, "YIELD_WAIT_SECONDS", 0.05)
    assert e.embed(100, yield_to=lambda: True) == 0  # busy the whole wait: give up, resume next tick
    assert e.embed(100, max_seconds=0) == 0  # tick budget spent
    calls = iter([True, False])
    assert e.embed(100, yield_to=lambda: next(calls, False)) == 3
    e.close()


def test_lane_workers_tick_one_at_a_time():
    import threading
    import time as _time

    active, peak, ticks, guard = [0], [0], [], threading.Lock()

    class Slow:
        def __init__(self, name):
            self.name = name

        def tick(self, state_db, limit, *, max_seconds=None, yield_to=None):
            with guard:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            _time.sleep(0.05)
            with guard:
                active[0] -= 1
                ticks.append((self.name, max_seconds))

    workers = [ep.EpisodeWorker(Slow(n), None, interval=0.01, tick_seconds=7.0).start() for n in ("owner", "assistant")]
    deadline = _time.monotonic() + 5
    while len({n for n, _ in ticks}) < 2 and _time.monotonic() < deadline:
        _time.sleep(0.01)
    for w in workers:
        w.stop()
    assert peak[0] == 1 and {n for n, _ in ticks} == {"owner", "assistant"} and all(s == 7.0 for _, s in ticks)


def test_a_running_tick_hands_the_lock_to_a_waiting_assist(tmp_path, state_db, monkeypatch):
    import threading
    import time as _time

    _add(state_db, [(100 + i, "tg1", "assistant", 1, 0, 1_760_500_000.0 + i,
                     f"Reply number {i} about the garden shed, the roof felt and the rain gutters.")
                    for i in range(40)])
    e = ep.Episodes(tmp_path / "e.db", model="hash-ngram")
    e.ingest(state_db)
    real = e.embedder()

    class Slow:
        def embed_passages(self, texts):
            _time.sleep(0.3)  # 40 chunks take ~3 s in all: far longer than the handover
            return real.embed_passages(texts)

    monkeypatch.setattr(e, "embedder", lambda: Slow())
    done = {}

    def tick():
        with ep.TICK_LOCK:
            done["n"] = e.embed(10_000, max_seconds=30)

    t = threading.Thread(target=tick)
    t.start()
    _time.sleep(0.4)
    started = _time.monotonic()
    ep.LOCK_WANTED.set()
    try:
        assert ep.TICK_LOCK.acquire(timeout=5)
        waited = _time.monotonic() - started
        ep.TICK_LOCK.release()
    finally:
        ep.LOCK_WANTED.clear()
    t.join(5)
    assert waited < 1.0 and 0 < done["n"] < 40 and e.pending() > 0
    e.close()
