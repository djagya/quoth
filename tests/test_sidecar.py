"""Quoth sidecar + Hermes provider plugin, end to end over a real Unix socket."""
from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import json
import time
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import threading
import types

import pytest

from quoth import hermes_install, sidecar
from quoth.db import open_db
from quoth.embed.backends import get_embedder
from quoth.embed.store import embed_chunks
from quoth.evidence_service import ReadOnlyEvidence
from quoth.ingest import ingest_vault
from quoth.ledger import core
from quoth.ledger.authored import find_authored
from quoth.refresh import sha
from quoth.serving import ServingSnapshot

REPO = Path(__file__).resolve().parents[1]


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
    db.executemany("INSERT INTO sessions(id, source) VALUES (?, ?)", [("tg1", "telegram"), ("tg2", "telegram")])
    db.executemany(
        "INSERT INTO messages(id, session_id, role, active, timestamp, content) VALUES (?,?,?,?,?,?)", [
            (1, "tg1", "user", 1, 1.0, "[Alex] I moved to Quendorf last spring."),
            (2, "tg1", "assistant", 1, 2.0, "Noted. Please accept 1 and 2 for you?"),
            (3, "tg1", "user", 1, 3.0, "yes, accept the Quendorf one and reject the tea one"),
            (4, "tg2", "user", 1, 4.0, "I prefer green tea in the morning."),
            (5, "tg1", "user", 1, 5.0, "ok, drop 2"),  # a review reply: the turn's approving message
        ])
    db.commit()
    db.close()
    return path


@pytest.fixture
def snapshot_db(vault, db_path):
    conn = open_db(db_path)
    ingest_vault(conn, vault)
    embed_chunks(conn, get_embedder("hash-ngram"), batch_size=8)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()
    db_path.with_suffix(".manifest.json").write_text(json.dumps({
        "db": str(db_path.resolve()), "db_sha256": sha(db_path), "db_bytes": db_path.stat().st_size,
        "status": "frozen_current_vault_candidate", "integrity": "ok",
        "schema": "validate_evidence_db"}))
    return db_path


@pytest.fixture
def served(tmp_path, state_db, snapshot_db):
    # AF_UNIX paths are capped near 104 bytes on macOS; pytest's tmp_path is too long.
    short = Path(tempfile.mkdtemp(prefix="mn"))
    app = sidecar.Quoth(ledger_path=tmp_path / "ledger.db", state_db=state_db,
                            evidence=ReadOnlyEvidence(ServingSnapshot(str(snapshot_db))),
                            model="hash-ngram")
    server = sidecar.SidecarServer(short / "s.sock", app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield app, server
    server.shutdown()
    server.server_close()
    app.close()
    shutil.rmtree(short, ignore_errors=True)


# ------------------------------------------------------------------ plugin loading

@dataclass(frozen=True)
class RecallStatus:
    provider_label: str
    count: int
    glyph: str = "*"


def _load_plugin(monkeypatch, plugin_dir: Path):
    """Import the plugin package as Hermes would, with a minimal stand-in for agent.memory_provider."""
    from abc import ABC

    mp = types.ModuleType("agent.memory_provider")

    class MemoryProvider(ABC):
        pass

    mp.MemoryProvider = MemoryProvider
    mp.RecallStatus = RecallStatus
    mp.is_trivial_prompt = lambda t: not (t or "").strip() or (t or "").strip().lower() in ("ok", "thanks")
    mp.spawn_context_thread = lambda target, *, name, daemon=True, args=(), kwargs=None: threading.Thread(
        target=target, args=args, kwargs=kwargs or {}, name=name, daemon=daemon)
    agent = types.ModuleType("agent")
    agent.memory_provider = mp
    monkeypatch.setitem(sys.modules, "agent", agent)
    monkeypatch.setitem(sys.modules, "agent.memory_provider", mp)
    name = "_quoth_test_plugin"
    spec = importlib.util.spec_from_file_location(name, plugin_dir / "__init__.py",
                                                  submodule_search_locations=[str(plugin_dir)])
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def provider(served, monkeypatch, tmp_path):
    _, server = served
    plugin = _load_plugin(monkeypatch, REPO / "hermes_plugin" / "quoth")

    class Ctx:
        provider = None

        def register_memory_provider(self, p):
            self.provider = p

    ctx = Ctx()
    plugin.register(ctx)
    p = ctx.provider
    monkeypatch.setenv("QUOTH_SOCKET", str(server.socket_path))
    assert p.name == "quoth" and p.is_available()
    p.initialize("tg1", hermes_home=str(tmp_path), platform="telegram", agent_context="primary")
    return p


def _call(p, tool, **args):
    return json.loads(p.handle_tool_call(tool, args))


# ------------------------------------------------------------------ tests

def test_find_authored_locates_only_owner_text_in_session(state_db):
    assert find_authored(state_db, "tg1", "moved to Quendorf") == "hermes:tg1:1"
    assert find_authored(state_db, "tg1", "Please accept 1 and 2") is None  # assistant text
    assert find_authored(state_db, "tg2", "moved to Quendorf") is None  # other session
    assert find_authored(state_db, "tg1", "short") is None


def test_propose_origin_comes_from_verified_quote_not_caller(provider, served):
    app, _ = served
    inferred = _call(provider, "propose_claim", text="Alex likes long walks.", kind="preference",
                     reason="mentioned twice")
    assert inferred["ok"] and inferred["claim"]["origin"] == "assistant_proposal"
    assert inferred["claim"]["review_status"] == "pending"

    stated = _call(provider, "propose_claim", text="Alex lives in Quendorf.", kind="identity",
                   reason="the owner said so", quote="moved to Quendorf", confidence=0.95)
    assert stated["claim"]["origin"] == "owner_statement"
    assert stated["claim"]["evidence_locator"] == "hermes:tg1:1"
    assert stated["auto_accepted"] is True  # verified identity fact, confidence >= 0.9

    forged = _call(provider, "propose_claim", text="Alex hates walks.", kind="preference",
                   reason="x", quote="Please accept 1 and 2")
    assert forged == {"ok": False, "error": "quote_not_found"}
    with app.lock:
        assert app.conn.execute("SELECT count(*) FROM claims").fetchone()[0] == 2


def test_review_needs_owners_approval_quote(provider):
    tea = _call(provider, "propose_claim", text="Alex drinks green tea.", kind="preference", reason="r")
    cid = tea["claim"]["id"]
    queue = _call(provider, "review_claims", action="queue")
    assert queue["ok"] and [c["id"] for c in queue["claims"]] == [cid]

    assert _call(provider, "review_claims", action="reject", ids=[cid]) == {
        "ok": False, "error": "approval_quote_required"}
    assert _call(provider, "review_claims", action="reject", ids=[cid],
                 approval_quote="Please accept 1 and 2")["error"] == "approval_quote_not_found"
    # Decisions take the review grammar, whole: a fragment of an older message approves nothing.
    assert _call(provider, "review_claims", action="reject", ids=[cid],
                 approval_quote="reject the tea one")["error"] == "approval_quote_not_found"
    done = _call(provider, "review_claims", action="reject", ids=[cid], approval_quote="ok, drop 2")
    assert done["ok"] and done["results"][0]["review_status"] == "rejected"
    assert done["approval_locator"] == "hermes:tg1:5"
    again = _call(provider, "review_claims", action="accept", ids=[cid], approval_quote="ok, drop 2")
    assert again["errors"] == [{"id": cid, "error": "not_pending"}]


def test_review_decisions_refused_outside_primary_session(provider):
    cid = _call(provider, "propose_claim", text="Alex owns a bike.", kind="environment",
                reason="r")["claim"]["id"]
    provider._context = "cron"
    out = _call(provider, "review_claims", action="accept", ids=[cid],
                approval_quote="accept the Quendorf one")
    assert out == {"ok": False, "error": "review_requires_primary_session"}
    assert _call(provider, "review_claims", action="queue")["ok"]


def test_prefetch_serves_accepted_claims_and_evidence_only(provider, served):
    app, _ = served
    _call(provider, "propose_claim", text="Alex keeps tomatoes and basil.", kind="environment",
          reason="pending only")
    assert "keeps tomatoes and basil" not in provider.prefetch("tomatoes basil", session_id="tg1")
    stated = _call(provider, "propose_claim", text="Alex grows tomatoes on the balcony.",
                   kind="environment", reason="r", quote="moved to Quendorf", confidence=0.95)
    assert stated["auto_accepted"]
    text = provider.prefetch("tomatoes balcony", session_id="tg1")
    assert text.startswith("<quoth-recall>") and "grows tomatoes on the balcony" in text
    assert "keeps tomatoes and basil" not in text
    assert "Vault evidence" in text and ".md:" in text
    status = provider.recall_status()
    assert status.provider_label == "Quoth" and status.count >= 2
    assert provider.prefetch("ok", session_id="tg1") == "" and provider.recall_status() is None
    with app.lock:
        used = app.conn.execute("SELECT use_count FROM claims WHERE id = ?",
                                (stated["claim"]["id"],)).fetchone()[0]
    assert used == 1


def test_evidence_tool_matches_read_only_mcp(provider, snapshot_db):
    from quoth.mcp_server import build_server
    fn = build_server(str(snapshot_db))._tool_manager.get_tool("evidence_bundle").fn
    via_plugin = _call(provider, "evidence_bundle", query="tomatoes basil", k=4)
    assert via_plugin.pop("ok") is True
    assert via_plugin == json.loads(fn(query="tomatoes basil", k=4, model="hash-ngram"))
    assert _call(provider, "evidence_bundle", query="", k=4)["error"] == "invalid_evidence_request"


def test_core_block_and_mirror(provider, served):
    app, _ = served
    assert "Quoth memory" in provider.system_prompt_block()
    provider.on_memory_write("add", "user", "Alex's cat is called Pixel.")
    for t in threading.enumerate():
        if t.name == "quoth-mirror":
            t.join(10)
    with app.lock:
        row = app.conn.execute("SELECT origin, kind, review_status, proposer FROM claims").fetchone()
    assert tuple(row) == ("imported", "unclassified", "pending", "builtin_user")
    # Accepted claims reach the core on the next session start.
    cid = _call(provider, "propose_claim", text="Alex lives in Quendorf.", kind="identity",
                reason="r", quote="moved to Quendorf", confidence=0.95)["claim"]["id"]
    provider.initialize("tg1", hermes_home="/nonexistent", agent_context="primary")
    assert "- Alex lives in Quendorf." in provider.system_prompt_block()
    with app.lock:
        assert core.get(app.conn, cid)["review_status"] == "accepted"


def test_sidecar_down_degrades_quietly(monkeypatch, tmp_path):
    plugin = _load_plugin(monkeypatch, REPO / "hermes_plugin" / "quoth")
    p = plugin.QuothMemoryProvider(socket_path=str(tmp_path / "missing.sock"))
    assert not p.is_available()
    p.initialize("tg1", hermes_home=str(tmp_path))
    assert p.prefetch("anything at all", session_id="tg1") == ""
    assert json.loads(p.handle_tool_call("evidence_bundle", {"query": "x"})) == {
        "ok": False, "error": "sidecar_unavailable"}
    assert "Quoth memory" in p.system_prompt_block()


def test_stale_socket_replaced_and_live_socket_refused(tmp_path, served):
    _, server = served
    with pytest.raises(RuntimeError, match="another sidecar"):
        sidecar.SidecarServer(server.socket_path, server.app)
    short = Path(tempfile.mkdtemp(prefix="mn"))
    try:
        stale = short / "s.sock"
        import socket as _s
        s = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)
        s.bind(str(stale))
        s.close()  # bound, never listening: a dead process's leftover
        srv = sidecar.SidecarServer(stale, server.app)
        assert oct(os.stat(stale).st_mode & 0o777) == "0o660"
        srv.server_close()
        assert not stale.exists()
    finally:
        shutil.rmtree(short, ignore_errors=True)


def test_plugin_install_copies_files_and_refuses_overwrite(tmp_path):
    out = hermes_install.install(tmp_path)
    target = tmp_path / "plugins" / "quoth"
    assert out["installed"] == str(target)
    assert sorted(p.name for p in target.iterdir()) == sorted(
        hermes_install.FILES + (hermes_install.MARKER, "skills"))
    assert sorted(p.name for p in (target / "skills").iterdir()) == sorted(hermes_install.SKILLS)
    with pytest.raises(FileExistsError):
        hermes_install.install(tmp_path)
    hermes_install.install(tmp_path, force=True)
    assert not [p for p in (tmp_path / "plugins").iterdir() if p.name.startswith(".")]
    manifest = (target / "plugin.yaml").read_text()
    from quoth import __version__
    assert f"version: {__version__}" in manifest


class _SlowEvidence:
    """Evidence stand-in that blocks until released, like a cold bge-m3 load."""

    def __init__(self):
        self.release = threading.Event()
        self.calls = 0

    def bundle(self, query, k, model):
        self.calls += 1
        self.release.wait(10)
        return {"hits": [{"locator": "n.md:1-2", "text": "slow hit", "note_date": None}]}


def test_prefetch_answers_with_claims_when_evidence_is_slow(tmp_path, state_db):
    slow = _SlowEvidence()
    app = sidecar.Quoth(ledger_path=tmp_path / "l.db", state_db=state_db, evidence=slow,
                            evidence_budget=0.2)
    try:
        app.propose({"text": "Alex lives in Quendorf.", "kind": "identity", "reason": "r",
                     "quote": "moved to Quendorf", "confidence": 0.95, "session_id": "tg1"})
        first = app.prefetch({"query": "where does Alex live Quendorf"})
        assert first["evidence_status"] == "slow" and first["claims"] == 1
        assert "lives in Quendorf" in first["text"] and "slow hit" not in first["text"]
        # Still running: the next turn skips evidence instead of queueing behind it.
        second = app.prefetch({"query": "Quendorf"})
        assert second["evidence_status"] == "busy" and slow.calls == 1
        slow.release.set()
        for _ in range(100):
            if app._evidence_future.done():
                break
            threading.Event().wait(0.02)
        third = app.prefetch({"query": "Quendorf again"})
        assert third["evidence_status"] == "ok" and "slow hit" in third["text"]
    finally:
        slow.release.set()
        app.close()


def test_client_disconnect_is_quiet(served, capsys):
    import socket as _s
    _, server = served
    s = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)
    s.connect(str(server.socket_path))
    body = b'{"query": "tomatoes basil"}'
    s.sendall(b"POST /v1/prefetch HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
              b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    s.setsockopt(_s.SOL_SOCKET, _s.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
    s.close()  # hang up before reading the reply
    threading.Event().wait(0.5)
    assert "Traceback" not in capsys.readouterr().err


def test_presort_suggest_then_apply_with_approval(provider, served):
    app, _ = served
    with app.lock:
        core.seed(app.conn, [{"text": "Alex drinks green tea."}, {"text": "Alex likes long walks."},
                             {"text": "Old fact nobody needs."}, {"text": "Alex drinks green tea daily."}])
    queue = _call(provider, "review_claims", action="queue", suggested=False)
    tea, walks, old, tea2 = [c["id"] for c in queue["claims"]]
    assert all(c["suggestion"] is None for c in queue["claims"])

    # Presort needs no approval and changes no claim.
    out = _call(provider, "review_claims", action="suggest", id=tea, suggestion="accept",
                kind="preference")
    assert out["ok"] and out["suggestion"]["suggested_kind"] == "preference"
    _call(provider, "review_claims", action="suggest", id=walks, suggestion="accept",
          kind="taste", text="Alex enjoys long walks.")
    _call(provider, "review_claims", action="suggest", id=old, suggestion="stale", note="outdated")
    assert _call(provider, "review_claims", action="suggest", id=tea2, suggestion="accept") == {
        "ok": False, "error": "kind_required"}  # unclassified needs a kind
    _call(provider, "review_claims", action="suggest", id=tea2, suggestion="duplicate", duplicate_of=tea)
    assert _call(provider, "review_claims", action="summary")["presort"] == {
        "accept": 2, "duplicate": 1, "stale": 1}
    assert _call(provider, "review_claims", action="queue", suggested=False)["claims"] == []
    with app.lock:
        assert {c["review_status"] for c in core.review_queue(app.conn, 10)} == {"pending"}

    assert _call(provider, "review_claims", action="apply", ids=[tea, walks, old, tea2])["error"] == \
        "approval_quote_required"
    done = _call(provider, "review_claims", action="apply", ids=[tea, walks, old, tea2],
                 approval_quote="ok, drop 2")
    assert not done["errors"]
    applied = {r["id"]: r for r in done["results"]}
    assert applied[old]["review_status"] == "rejected"
    assert applied[tea2]["review_status"] == "rejected"
    with app.lock:
        accepted = {c["text"]: c["kind"] for c in core.recall(app.conn, "Alex tea walks", k=10)}
        assert core.get(app.conn, tea2)["review_note"].startswith(f"duplicate of #{tea}")
    assert accepted == {"Alex drinks green tea.": "preference", "Alex enjoys long walks.": "taste"}
    assert _call(provider, "review_claims", action="summary")["presort"] == {}


def test_presort_rejects_bad_targets(provider, served):
    app, _ = served
    with app.lock:
        core.seed(app.conn, [{"text": "Fact one."}])
        cid = core.review_queue(app.conn, 1)[0]["id"]
    assert _call(provider, "review_claims", action="suggest", id=cid, suggestion="duplicate",
                 duplicate_of=cid)["error"] == "duplicate_of_not_current"
    assert _call(provider, "review_claims", action="suggest", id=999, suggestion="stale")["error"] == "not_found"
    assert _call(provider, "review_claims", action="apply", ids=[cid],
                 approval_quote="ok, drop 2")["errors"] == [{"id": cid, "error": "no_suggestion"}]


def test_dense_claim_recall_finds_meaning_without_shared_words(provider, served):
    app, _ = served
    stated = _call(provider, "propose_claim", text="Alex lives in Quendorf.", kind="identity",
                   reason="r", quote="moved to Quendorf", confidence=0.95)
    assert stated["auto_accepted"]
    with app.lock:
        core.seed(app.conn, [{"text": "Quendorf pending fact."}])  # pending: never served
    assert app.claim_index is not None
    while app.sync_claims():
        pass
    assert len(app.claim_index) == 1
    query = "Quendorfish neighbourhoods"
    with app.lock:
        assert core.recall(app.conn, query) == []  # no shared word
    app.claim_index.min_similarity = 0.2  # hash-ngram test vectors are coarser than bge-m3
    text = provider.prefetch(query, session_id="tg1")
    assert "lives in Quendorf" in text and "pending fact" not in text
    # A retired claim leaves the index on the next sync and is never served from a stale vector.
    with app.lock:
        core.forget(app.conn, stated["claim"]["id"], reason="moved")
    assert "lives in Quendorf" not in provider.prefetch(query + " again", session_id="tg1")
    app.sync_claims()
    assert len(app.claim_index) == 0


def test_memory_command_views(provider, served, monkeypatch):
    app, server = served
    plugin = sys.modules["_quoth_test_plugin"]
    commands = {}

    class Ctx:
        def register_memory_provider(self, p):
            pass

        def register_command(self, name, handler, description, args_hint):
            assert not args_hint.startswith("<")
            commands[name] = handler

    plugin.register(Ctx())
    assert "memory" not in commands  # Hermes has a built-in /memory
    memory = commands["quoth"]
    stated = _call(provider, "propose_claim", text="Alex lives in Quendorf.", kind="identity",
                   reason="r", quote="moved to Quendorf", confidence=0.95)
    cid = stated["claim"]["id"]
    with app.lock:
        core.seed(app.conn, [{"text": "Alex drinks green tea."}])
        tea = core.review_queue(app.conn, 1)[0]["id"]
    _call(provider, "review_claims", action="suggest", id=tea, suggestion="accept", kind="preference")

    health = memory("")
    assert "1 accepted, 1 pending" in health and "Presort: accept 1" in health and "/quoth core" in health
    assert "- Alex lives in Quendorf." in memory("core")
    queue = memory("queue")
    assert "Memory review: 1 waiting" in queue and "remember 1" in queue and "Other 1" in queue
    assert "say 'review memory' in chat" in queue.lower() and "[unclassified" not in queue
    assert f"#{tea} Alex drinks green tea. — remember" in memory("queue remember")
    assert memory("queue accept") == memory("queue remember")
    assert memory("queue oth").startswith("Other: 1 waiting\n")  # topic, by prefix
    assert memory("queue zebra").startswith("No group called 'zebra'")
    item = memory(f"item {cid}")
    assert "origin: owner_statement (quote verified)" in item and "auto_accepted by auto" in item
    assert "green tea" in memory("find tea") and memory("find zebra") == "Nothing found."
    recall = memory("recall where does Alex live Quendorf")
    assert f"#{cid} [identity] Alex lives in Quendorf." in recall and "Vault (" in recall
    with app.lock:  # recall view is a dry run
        assert core.get(app.conn, cid)["use_count"] == 0
    assert memory("nonsense") .startswith("/quoth —")
    monkeypatch.setenv("QUOTH_SOCKET", "/nonexistent.sock")
    assert memory("") == "Quoth sidecar is not reachable right now."


def test_agreed_commitment_is_assistants_pending_proposal(provider, served):
    app, _ = served
    out = _call(provider, "propose_claim", text="The assistant reviews the memory queue every Sunday.",
                kind="project_state", reason="The owner agreed to the weekly review", confidence=0.99,
                agreement_quote="accept the Quendorf one")
    assert out["decision"] == "add" and not out["auto_accepted"]
    claim = out["claim"]
    assert claim["authority"] == "assistant" and claim["review_status"] == "pending"
    with app.lock:
        row = core.get(app.conn, claim["id"])
    assert row["origin"] == "assistant_proposal" and row["evidence_locator"] == "hermes:tg1:3"
    assert row["reason"].startswith("The owner agreed (hermes:tg1:3)")
    # Agreement must be the owner's own words in this chat, and it is one or the other.
    assert _call(provider, "propose_claim", text="x y z", kind="pointer", reason="r",
                 agreement_quote="words the owner never wrote")["error"] == "agreement_quote_not_found"
    assert _call(provider, "propose_claim", text="x y z", kind="pointer", reason="r",
                 quote="moved to Quendorf", agreement_quote="accept the Quendorf one")["error"] == \
        "quote_or_agreement"


def test_prefetch_log_records_turns_and_tool_calls_without_text(provider, served, tmp_path):
    from quoth.evals.online import online
    from quoth.prefetch_log import PrefetchLog, session_hash
    app, _ = served
    log_dir = tmp_path / "plog"
    app.prefetch_log = PrefetchLog(log_dir, days=30)
    query = "where do tomatoes and basil grow in the garden"
    assert provider.prefetch(query, session_id="tg1")
    assert _call(provider, "evidence_bundle", query="south bed tomatoes", k=3)["ok"]
    assert _call(provider, "propose_claim", text="Alex likes basil.", kind="taste", reason="r")["ok"]
    app.prefetch_log.flush()
    files = list(log_dir.glob("prefetch-*.jsonl"))
    assert len(files) == 1 and oct(files[0].stat().st_mode & 0o777) == "0o600"
    raw = files[0].read_text()
    assert "tomatoes" not in raw and "basil" not in raw and "tg1" not in raw
    turn, tool = [json.loads(line) for line in raw.splitlines()]
    assert turn["type"] == "prefetch" and turn["session"] == session_hash("tg1")
    assert turn["query_len"] == len(query) and len(turn["query_sha256"]) == 64
    assert set(turn["stores"]) >= {"claims", "vault"} and turn["status"]["vault"] == "ok"
    assert all({"locator", "score", "injected"} <= set(h) for h in turn["stores"]["vault"])
    assert {"total", "claims_lexical", "vault"} <= set(turn["ms"])
    assert tool["type"] == "tool" and tool["tool"] == "evidence_bundle"
    assert tool["session"] == turn["session"] and tool["k"] == 3 and tool["results"]
    report = online(log_dir)
    assert report["turns"] == 1 and report["probable_miss_rate"] == 1.0
    assert report["injected_rate"]["vault"] == 1.0 and "tomatoes" not in json.dumps(report)


def test_plugin_socket_rule(monkeypatch, tmp_path):
    """QUOTH_SOCKET, else the provider config, else $QUOTH_HOME/quoth.sock, else the XDG default."""
    plugin = _load_plugin(monkeypatch, REPO / "hermes_plugin" / "quoth")
    for name in ("QUOTH_SOCKET", "QUOTH_HOME", "XDG_DATA_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert plugin._socket_path(None) == str(tmp_path / "home" / ".local" / "share" / "quoth" / "quoth.sock")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert plugin._socket_path(None) == str(tmp_path / "xdg" / "quoth" / "quoth.sock")
    monkeypatch.setenv("QUOTH_HOME", str(tmp_path / "qh"))
    assert plugin._socket_path(None) == str(tmp_path / "qh" / "quoth.sock")
    # The sidecar's own default resolves the same way.
    assert str(sidecar.default_socket()) == str(tmp_path / "qh" / "quoth.sock")
    (tmp_path / "quoth.json").write_text('{"socket": "/cfg/q.sock"}')
    assert plugin._socket_path(str(tmp_path)) == "/cfg/q.sock"
    monkeypatch.setenv("QUOTH_SOCKET", "/env/q.sock")
    assert plugin._socket_path(str(tmp_path)) == "/env/q.sock"


def test_plugin_save_config_keeps_other_settings(monkeypatch, tmp_path):
    plugin = _load_plugin(monkeypatch, REPO / "hermes_plugin" / "quoth")
    (tmp_path / "quoth.json").write_text('{"auto_recall_platforms": ["cli"]}')
    plugin.QuothMemoryProvider().save_config({"socket": "/s/q.sock"}, str(tmp_path))
    assert json.loads((tmp_path / "quoth.json").read_text()) == {
        "auto_recall_platforms": ["cli"], "socket": "/s/q.sock"}


def test_tool_schemas_speak_of_the_owner_and_bound_their_inputs(monkeypatch):
    plugin = _load_plugin(monkeypatch, REPO / "hermes_plugin" / "quoth")
    schemas = plugin.QuothMemoryProvider("/nonexistent.sock").get_tool_schemas()
    text = json.dumps(schemas) + plugin.GUIDE
    assert "Telegram" not in text and "the owner" in text
    propose = plugin.PROPOSE_SCHEMA["parameters"]["properties"]
    assert propose["text"]["maxLength"] == core.MAX_CLAIM_CHARS
    for field in ("quote", "agreement_quote"):
        assert (propose[field]["minLength"], propose[field]["maxLength"]) == (8, 500)
    review = plugin.REVIEW_SCHEMA["parameters"]["properties"]
    assert review["evidence_locator"]["maxLength"] == core.MAX_LOCATOR_CHARS
    assert review["text"]["maxLength"] == core.MAX_CLAIM_CHARS
    assert all(b in review["bucket"]["description"] for b in core.JUNK_BUCKETS)
    assert plugin.EPISODE_SCHEMA["parameters"]["properties"]["who"]["enum"] == ["assistant", "owner"]
    for schema in (plugin.EVIDENCE_SCHEMA, plugin.EPISODE_SCHEMA):
        assert schema["parameters"]["properties"]["query"]["maxLength"] == 2048


def test_plugin_proposer_names_the_assistant_and_its_context(monkeypatch):
    plugin = _load_plugin(monkeypatch, REPO / "hermes_plugin" / "quoth")
    sent = []

    class Client:
        def post(self, route, body, timeout):
            sent.append((route, body))
            return {"ok": True}

    p = plugin.QuothMemoryProvider("/nonexistent.sock")
    p._client = Client()
    p.handle_tool_call("propose_claim", {"text": "t", "kind": "taste", "reason": "r"})
    p._context = "cron"
    p.handle_tool_call("review_claims", {"action": "queue"})
    p.handle_tool_call("episode_search", {"query": "q"})
    assert sent[0][1]["proposer"] == "assistant"
    assert sent[1][1]["proposer"] == "assistant:cron" and sent[1][1]["context"] == "cron"
    assert sent[2][1]["who"] == "assistant"


@pytest.mark.parametrize("injected", [
    "[kanban] wake: task 12 is due",
    "[Cron delivery: Morning Brief]\nYou moved to Quendorf last spring.",
    "[Continuing toward your standing goal]\nI moved to Quendorf last spring.",
    "[IMPORTANT: Background process proc_a exited (exit code 0).]",
    "[Alex] [kanban] wake: I moved to Quendorf last spring.",
    "[OUT-OF-BAND USER MESSAGE — a direct message] I moved to Quendorf last spring.",
    "A background subagent you dispatched earlier has finished. I moved to Quendorf.",
])
def test_injected_user_turns_never_count_as_owners_words(tmp_path, injected):
    from quoth.ledger.authored import StateDbVerifier, authored_text
    db = sqlite3.connect(tmp_path / "s.db")
    db.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, active INTEGER,
            compacted INTEGER DEFAULT 0, timestamp REAL, content TEXT);
        INSERT INTO sessions VALUES ('tg1', 'telegram');
    """)
    db.execute("INSERT INTO messages VALUES (1, 'tg1', 'user', 1, 0, 1.0, ?)", (injected,))
    db.commit()
    db.close()
    assert authored_text(tmp_path / "s.db", "tg1", 1) is None
    assert not StateDbVerifier(tmp_path / "s.db")("hermes:tg1:1", "moved to Quendorf")


def test_injected_display_kind_never_counts_as_owners_words(tmp_path):
    from quoth.ledger.authored import authored_text
    db = sqlite3.connect(tmp_path / "s.db")
    db.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL);
        CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, active INTEGER,
            compacted INTEGER DEFAULT 0, timestamp REAL, content TEXT, display_kind TEXT);
        INSERT INTO sessions VALUES ('tg1', 'telegram');
        INSERT INTO messages VALUES (1, 'tg1', 'user', 1, 0, 1.0, 'I moved to Quendorf last spring.', 'internal_notification');
        INSERT INTO messages VALUES (2, 'tg1', 'user', 1, 0, 2.0, 'I moved to Quendorf last spring.', NULL);
    """)
    db.commit()
    db.close()
    assert authored_text(tmp_path / "s.db", "tg1", 1) is None
    assert authored_text(tmp_path / "s.db", "tg1", 2) == "I moved to Quendorf last spring."


class _DelayedEvidence:
    def __init__(self, delay):
        self.delay = delay

    def bundle(self, query, k, model):
        time.sleep(self.delay)
        return {"hits": [{"locator": "n.md:1-2", "text": "late hit", "note_date": None}]}


def test_prefetch_query_is_owners_payload_only():
    assert sidecar.prefetch_query("[Alex] where did I move?") == "where did I move?"
    assert sidecar.prefetch_query('[Replying to: "The assistant said a long thing"]\n\n[Alex] and the other one?') == \
        "and the other one?"
    for injected in ("[kanban] wake: task 12 is due", "[Cron delivery: Morning Brief]\nreport",
                     "[IMPORTANT: Background process proc_a exited (exit code 0).]", "   "):
        assert sidecar.prefetch_query(injected) == ""
    assert sidecar.prefetch_query("plain eval question?") == "plain eval question?"
    assert len(sidecar.prefetch_query("x" * 20000)) == sidecar.QUERY_MAX_CHARS


def test_injected_turn_skips_prefetch_and_is_logged(tmp_path, state_db):
    from quoth.prefetch_log import PrefetchLog
    slow = _SlowEvidence()
    plog = PrefetchLog(tmp_path / "log")
    app = sidecar.Quoth(ledger_path=tmp_path / "l.db", state_db=state_db, evidence=slow, prefetch_log=plog)
    try:
        out = app.prefetch({"query": "[kanban] wake: task 12 is due", "session_id": "k1"})
        assert out["evidence_status"] == "skipped" and slow.calls == 0
        plog.flush()
        records = [json.loads(line) for f in (tmp_path / "log").glob("*.jsonl") for line in f.open()]
        assert [r["type"] for r in records] == ["skip"]
    finally:
        slow.release.set()
        app.close()


def test_dry_run_can_wait_for_the_vault_live_turns_cannot(tmp_path, state_db):
    app = sidecar.Quoth(ledger_path=tmp_path / "l.db", state_db=state_db,
                        evidence=_DelayedEvidence(0.4), evidence_budget=0.1)
    try:
        assert app.prefetch({"query": "where is the shed"})["evidence_status"] == "slow"
        time.sleep(0.5)  # let the slow call finish so the next one isn't "busy"
        dry = app.prefetch({"query": "where is the garden shed", "dry_run": True, "evidence_budget": 5})
        assert dry["evidence_status"] == "ok" and "late hit" in dry["text"]
        time.sleep(0.1)
        live = app.prefetch({"query": "where is the shed now", "evidence_budget": 5})
        assert live["evidence_status"] == "slow"  # only dry runs may extend the budget
    finally:
        app.close()


def test_auto_recall_only_in_owners_own_chats(served, monkeypatch, tmp_path):
    app, server = served
    plugin = _load_plugin(monkeypatch, REPO / "hermes_plugin" / "quoth")
    _call_plugin = plugin.QuothMemoryProvider
    with app.lock:
        core.seed(app.conn, [{"text": "Alex lives in Quendorf."}])
    seen = []
    real = app.prefetch
    monkeypatch.setattr(app, "prefetch", lambda body: seen.append(body) or real(body))
    for platform, context, expected in [("telegram", "primary", 1), ("cron", "cron", 0),
                                        ("subagent", "subagent", 0), ("kanban", "primary", 0),
                                        ("cli", "primary", 0)]:
        seen.clear()
        p = _call_plugin(socket_path=str(server.socket_path))
        p.initialize("s1", hermes_home=str(tmp_path), platform=platform, agent_context=context)
        p.prefetch("where does Alex live these days?", session_id="s1")
        assert len(seen) == expected, (platform, context)
        # Tools stay available everywhere: memory on request, never pushed.
        assert "evidence_bundle" in [s["name"] for s in p.get_tool_schemas()]
    (tmp_path / "quoth.json").write_text('{"auto_recall_platforms": ["telegram", "cli"]}')
    seen.clear()
    p = _call_plugin(socket_path=str(server.socket_path))
    p.initialize("s1", hermes_home=str(tmp_path), platform="cli", agent_context="primary")
    p.prefetch("where does Alex live these days?", session_id="s1")
    assert len(seen) == 1


# ------------------------------------------------------------------ HTTP surface

def _raw(sock_path, request: bytes) -> tuple[int, dict, dict]:
    """Send raw bytes to the sidecar socket; (status, headers, JSON body or {})."""
    import socket as _s
    with _s.socket(_s.AF_UNIX, _s.SOCK_STREAM) as conn:
        conn.settimeout(10)
        conn.connect(str(sock_path))
        conn.sendall(request)
        raw = b""
        while chunk := conn.recv(65536):
            raw += chunk
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = {k.lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:])}
    return int(lines[0].split()[1]), headers, json.loads(body) if body else {}


def _req(method: str, path: str, body: bytes = b"", headers: dict | None = None) -> bytes:
    lines = [f"{method} {path} HTTP/1.0", "Host: quoth"]
    lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def test_http_content_length_is_validated_inside_error_handling(served):
    _, server = served
    for bad in ("abc", "-5", "1.5"):
        status, _, body = _raw(server.socket_path, _req("POST", "/v1/view", b"{}", {"Content-Length": bad}))
        assert (status, body) == (400, {"ok": False, "error": "invalid_length"}), bad
    status, _, body = _raw(server.socket_path, _req(
        "POST", "/v1/view", b"", {"Content-Length": str(sidecar.MAX_BODY + 1)}))
    assert (status, body["error"]) == (413, "body_too_large")
    # The server keeps answering afterwards.
    status, _, body = _raw(server.socket_path, _req("GET", "/v1/core"))
    assert status == 200 and body["ok"]


def test_http_unknown_method_on_known_path_is_405_json_with_allow(served):
    _, server = served
    for method in ("PUT", "DELETE", "PATCH", "OPTIONS", "BREW"):
        status, headers, body = _raw(server.socket_path, _req(method, "/v1/review", b"{}",
                                                              {"Content-Length": "2"}))
        assert status == 405, method
        assert headers["allow"] == "POST" and headers["content-type"].startswith("application/json")
        assert body == {"ok": False, "error": "method_not_allowed"}
    status, headers, _ = _raw(server.socket_path, _req("POST", "/v1/health", b"{}", {"Content-Length": "2"}))
    assert status == 405 and headers["allow"] == "GET"
    status, headers, body = _raw(server.socket_path, _req("HEAD", "/v1/core"))
    assert status == 405 and headers["allow"] == "GET" and body == {}
    # Unknown paths stay 404, in JSON, whatever the method.
    for method in ("GET", "PUT", "BREW"):
        status, _, body = _raw(server.socket_path, _req(method, "/v1/nope"))
        assert (status, body) == (404, {"ok": False, "error": "not_found"}), method


def test_http_query_string_does_not_change_routing(served):
    _, server = served
    status, _, body = _raw(server.socket_path, _req("GET", "/v1/core?x=1&y=2"))
    assert status == 200 and body["ok"]
    status, _, body = _raw(server.socket_path, _req("POST", "/v1/view?args=core", b"{}", {"Content-Length": "2"}))
    assert status == 200 and "/quoth" in body["text"]  # the help view: the query string is not input


def test_http_wrong_types_are_400_with_field_and_bugs_are_500_internal(served, monkeypatch):
    app, server = served

    def post(route, payload):
        data = json.dumps(payload).encode()
        return _raw(server.socket_path, _req("POST", route, data, {"Content-Length": str(len(data))}))

    assert post("/v1/review", {"action": "queue", "context": "primary", "limit": "many"})[2] == {
        "ok": False, "error": "invalid_limit"}
    assert post("/v1/review", {"action": "queue", "context": "primary", "limit": True})[2]["error"] == "invalid_limit"
    assert post("/v1/propose", {"text": ["x"], "kind": "taste"})[2]["error"] == "invalid_text"
    assert post("/v1/propose", {"text": "x", "kind": "taste", "confidence": "high"})[2]["error"] == \
        "invalid_confidence"
    assert post("/v1/prefetch", {"query": "q", "k_evidence": {}})[2]["error"] == "invalid_k_evidence"
    assert post("/v1/evidence", {"query": "q", "k": "ten"})[2]["error"] == "invalid_k"
    assert post("/v1/episodes", {"query": 3})[0] in (400, 503)
    assert post("/v1/review", {"action": "suggest", "context": "primary", "id": "x"})[2]["error"] == "invalid_id"
    assert post("/v1/review", {"action": "accept", "context": "primary", "ids": "1,2"})[2]["error"] == "invalid_ids"

    def broken(body):
        raise TypeError("a bug, not a bad request")
    monkeypatch.setattr(app, "core", broken)
    status, _, body = _raw(server.socket_path, _req("GET", "/v1/core"))
    assert (status, body) == (500, {"ok": False, "error": "internal"})


def test_http_limits_are_clamped(served):
    app, _ = served
    for i in range(3):
        app.propose({"text": f"Alex fact number {i}.", "kind": "taste", "reason": "r"})
    assert len(app.review({"action": "queue", "context": "primary", "limit": 0})["claims"]) == 1
    assert len(app.review({"action": "queue", "context": "primary", "limit": -7})["claims"]) == 1
    assert len(app.review({"action": "queue", "context": "primary", "limit": 10_000})["claims"]) == 3
    assert len(app.review({"action": "queue", "context": "primary", "offset": -3})["claims"]) == 3
    digest = app.review({"action": "digest", "context": "primary", "limit": 10_000})
    assert digest["pending"] == 3


def test_review_requires_an_explicit_context(served):
    app, _ = served
    for action in ("queue", "digest", "accept"):
        with pytest.raises(sidecar.SidecarError) as err:
            app.review({"action": action, "ids": [1], "approval_quote": "ok"})
        assert err.value.code == "context_required", action
    with pytest.raises(sidecar.SidecarError) as err:
        app.review({"action": "accept", "context": "cron", "ids": [1], "approval_quote": "ok"})
    assert err.value.code == "review_requires_primary_session"


def test_health_is_503_only_when_evidence_is_unavailable(served):
    app, server = served
    status, _, body = _raw(server.socket_path, _req("GET", "/v1/health"))
    assert status == 200 and body["schema"] == "quoth-sidecar/v1" and body["status"] == "green"
    assert set(body) >= {"episodes", "owner_episodes", "ledger", "evidence"}

    class Gone:
        def health(self):
            raise OSError("snapshot gone")
    app.evidence.snapshot, real = Gone(), app.evidence.snapshot
    try:
        status, _, body = _raw(server.socket_path, _req("GET", "/v1/health"))
    finally:
        app.evidence.snapshot = real
    assert status == 503 and body["ok"] is False and body["error"] == "unhealthy"
    assert body["status"] == "red" and body["evidence"] == {"status": "unavailable"}


def test_module_docstring_states_the_trust_boundary():
    doc = sidecar.__doc__
    assert "Trust boundary" in doc and "permissions" in doc and "socket" in doc
