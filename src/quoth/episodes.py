"""Conversation lanes: searchable records of what was said in Hermes Telegram chats.

Two lanes, each in its own small mutable SQLite file with its own schema, watermark
and heartbeat; neither touches the serving snapshot or the ledger:

- ``assistant`` (default ``$QUOTH_HOME/episodes.db``): what the assistant said.
  Assistant-origin, never the owner's authority, never evidence or memory; by default
  reachable only through the ``episode_search`` tool, not injected into recall.
- ``owner`` (default ``$QUOTH_HOME/owner-episodes.db``): what the owner wrote, as
  owner-authored payload only (transport wrappers, quoted replies of the assistant's
  text, machine captions and synthetic runtime messages are dropped, failing closed).
  The owner's words at the time: verbatim, but unreviewed and possibly outdated, so
  never accepted memory.

A DB records its lane on creation and refuses to be opened as the other one.

- ``ingest``: incremental, read-only scan of Hermes ``state.db`` (``mode=ro``):
  rows of the lane's role of Telegram sessions, active or compaction originals,
  newer than a stored watermark (max ``messages.id`` seen). Credential redaction as
  in session ingest; empty or tool-call-only rows are skipped.
- ``embed``: passage vectors for pending chunks, bounded per call so a background
  tick never starves serving.
- ``search``: brute-force cosine over the stored vectors (numpy), with an
  in-memory matrix cached until the rows change.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import logging
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time

import numpy as np

from . import paths
from .ledger.authored import (ALLOWED_SOURCES, SYNTHETIC_PREFIXES, _authored_payload, _clean_text,
                              _synthetic_reason, _transport_text, injected_sql, not_injected_sql)
from .redact import redact

log = logging.getLogger(__name__)

ORIGIN = "assistant"
ANSWER_USE = ("Your own earlier words (assistant-origin). Not the owner's statements, not vault "
              "evidence, not accepted memory; never cite them as authority.")
# lane -> (hit origin, hit authority, answer_use)
LANES = {
    "assistant": (ORIGIN, "none", ANSWER_USE),
    "owner": ("owner", "unreviewed", (
        "The owner's own earlier words (verbatim, credential-redacted). What the owner wrote at "
        "the time: may be outdated and is not accepted memory; to keep a fact, propose it with "
        "the owner's quote.")),
}
# lane -> the Hermes state.db message role it reads
MESSAGE_ROLE = {"assistant": "assistant", "owner": "user"}
DEFAULT_DB = {"assistant": "episodes.db", "owner": "owner-episodes.db"}


def default_path(role: str = "assistant") -> Path:
    """The lane's DB under QUOTH_HOME: `episodes.db` (assistant) or `owner-episodes.db` (owner)."""
    return paths.state_file(DEFAULT_DB[role])
CHUNK_CHARS = 1200
CHUNK_OVERLAP = 200
MIN_CHARS = 12  # "Ok.", "Done." carry nothing worth finding again
SCAN_PAGE = 2000
EMBED_BATCH = 64  # default chunk cap for one-off `embed` calls
WORKER_TICK_LIMIT = 4096  # background ticks: EMBED_TICK_SECONDS is the real bound, not the count
# Cumulative ingest counters kept in episode_state as total:<name> (counts only, no content).
TOTAL_COUNTERS = ("added", "skipped", "duplicates", "injected")
EMBED_SUB_BATCH = 4  # small, so a live query never waits long behind a background batch
EMBED_TICK_SECONDS = 60.0  # CPU budget of one background tick
# Lanes share one embedding model and the CPU: their background ticks run one at a time.
TICK_LOCK = threading.Lock()
# Set while someone else (the review assist) waits for TICK_LOCK: a running tick stops at
# the next batch boundary instead of holding the lock for its whole budget.
LOCK_WANTED = threading.Event()
YIELD_WAIT_SECONDS = 30.0  # how long a tick waits for live traffic to pass before giving up
MAX_K = 20
MAX_QUERY = 2048

_MIGRATIONS = (
    (1, "episodes_init", """
        CREATE TABLE episode_messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            message_id INTEGER NOT NULL,
            ts REAL NOT NULL,
            text TEXT NOT NULL,
            redactions INTEGER NOT NULL DEFAULT 0,
            ingested_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            UNIQUE (session_id, message_id)
        );
        CREATE TABLE episode_chunks (
            id INTEGER PRIMARY KEY,
            message_ref INTEGER NOT NULL REFERENCES episode_messages(id) ON DELETE CASCADE,
            seq INTEGER NOT NULL,
            text TEXT NOT NULL,
            model TEXT,
            dim INTEGER,
            vector BLOB,
            UNIQUE (message_ref, seq)
        );
        CREATE INDEX episode_chunks_model ON episode_chunks(model);
        CREATE TABLE episode_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );
    """),
    (2, "episodes_text_sha", """
        ALTER TABLE episode_messages ADD COLUMN text_sha TEXT;
        CREATE INDEX episode_messages_text_sha ON episode_messages(text_sha)
    """),
    # The lane of what the owner wrote was stored as its message role, 'user'.
    (3, "episodes_owner_lane", """
        UPDATE episode_state SET value = 'owner' WHERE key = 'role' AND value = 'user'
    """),
)

_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
# Bare key ids that `redact.redact` (label and prefix based) misses.
_EXTRA_REDACTIONS = (
    ("aws_key_id", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    ("op_ref_secret", re.compile(r"\bops_[A-Za-z0-9]{20,}\b")),
)


def _redact(text: str) -> tuple[str, int]:
    text, hits = redact(text)
    for label, pat in _EXTRA_REDACTIONS:
        text, n = pat.subn(f"[REDACTED:{label}]", text)
        hits += n
    return text, hits
_BLANKS_RE = re.compile(r"\n{3,}")


def clean_assistant(content) -> tuple[str, int]:
    """Assistant text as the owner saw it, credential-redacted; ("", 0) when nothing to keep."""
    text = _clean_text(content)
    try:
        text = _transport_text(text)
    except ValueError:
        return "", 0
    text = _THINK_RE.sub("", text).strip()
    if len(text) < MIN_CHARS or any(text.startswith(p) for p in SYNTHETIC_PREFIXES):
        return "", 0
    text = _BLANKS_RE.sub("\n\n", text)
    return _redact(text)


def clean_user(content, source: str) -> tuple[str, int]:
    """Owner-authored payload of a user message, credential-redacted; ("", 0) when nothing to keep.

    Reuses the ledger's quote rules, so only text the owner typed (or dictated) survives:
    a quoted reply of the assistant's message, attachment captions and runtime notes are
    dropped.
    """
    text = _clean_text(content)
    if _synthetic_reason(text, source) is not None:
        return "", 0
    try:
        authored, _ = _authored_payload(text)
    except ValueError:
        return "", 0
    authored = re.sub(r"(?m)^\[image attachment\]\s*$", "", authored).strip()
    if len(authored) < MIN_CHARS or authored == "[non-text attachment]":
        return "", 0
    return _redact(_BLANKS_RE.sub("\n\n", authored))


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """~`size`-char windows with `overlap`, cut at whitespace where possible."""
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    out, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            cut = max(text.rfind(" ", start + size // 2, end), text.rfind("\n", start + size // 2, end))
            if cut > start:
                end = cut
        piece = text[start:end].strip()
        if piece:
            out.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
        space = text.find(" ", start, end)
        if space != -1:
            start = space + 1
    return out


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def open_episodes(path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the episodes DB and apply its own migrations."""
    p = Path(path)
    if str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("CREATE TABLE IF NOT EXISTS episodes_migrations (version INTEGER PRIMARY KEY,"
                 " name TEXT NOT NULL, applied_at TEXT NOT NULL"
                 " DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))")
    applied = {r[0]: r[1] for r in conn.execute("SELECT version, name FROM episodes_migrations")}
    if set(applied) - {v for v, _, _ in _MIGRATIONS}:
        conn.close()
        raise RuntimeError("episodes DB is newer than this code")
    for version, name, sql in _MIGRATIONS:
        if version in applied:
            if applied[version] != name:
                conn.close()
                raise RuntimeError(f"episodes migration {version} name mismatch")
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in (s.strip() for s in sql.split(";")):
                if statement:
                    conn.execute(statement)
            conn.execute("INSERT INTO episodes_migrations(version, name) VALUES (?, ?)", (version, name))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    _dedupe(conn)
    return conn


def _text_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dedupe(conn: sqlite3.Connection) -> int:
    """Fill missing text hashes, then keep one row per identical text; returns rows removed.

    Hermes re-saves the protected tail of a conversation at every compaction, so the same
    message appears under several ids. Keep the copy with the most embedded chunks, then
    the earliest.
    """
    while True:
        rows = conn.execute("SELECT id, text FROM episode_messages WHERE text_sha IS NULL LIMIT 2000").fetchall()
        if not rows:
            break
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany("UPDATE episode_messages SET text_sha = ? WHERE id = ?",
                         [(_text_sha(r["text"]), r["id"]) for r in rows])
        conn.execute("COMMIT")
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute("""
            DELETE FROM episode_messages WHERE id IN (
              SELECT id FROM (
                SELECT m.id, ROW_NUMBER() OVER (
                  PARTITION BY m.text_sha
                  ORDER BY (SELECT count(*) FROM episode_chunks c
                            WHERE c.message_ref = m.id AND c.vector IS NOT NULL) DESC, m.ts, m.id) AS rn
                FROM episode_messages m) WHERE rn > 1)""")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return cur.rowcount


class Episodes:
    """One episodes DB: ingest from state.db, bounded embedding, cosine search.

    Thread-safe: one SQLite connection behind a lock; embedding runs outside it.
    `embedder_factory()` returns the embedder for `model` (the sidecar passes its
    shared query embedder so bge-m3 is loaded once); `query_vector(text)`, when given,
    replaces `embed_query` (the sidecar passes its query-vector cache, so a prefetch
    embeds the turn once for both evidence and episodes).
    """

    def __init__(self, path: str | Path | None = None, *, model: str = "bge-m3",
                 embedder_factory=None, query_vector=None, max_age_days: float | None = None,
                 role: str = "assistant"):
        if role not in LANES:
            raise ValueError(f"unknown episodes role: {role}")
        self.path = Path(path) if path is not None else default_path(role)
        self.role = role
        # Embed only replies newer than this (None = all). Older ones stay ingested, unembedded.
        self.max_age_days = max_age_days
        self.conn = open_episodes(self.path)
        self.model = model
        self.lock = threading.Lock()
        self._embedder_factory = embedder_factory
        self._query_vector = query_vector
        self._embedder = None
        self._gen = 0  # bumped on every local write that changes vectors
        self._matrix = None  # (key, chunk_ids, mat)
        stored = self._get_state("role")
        if stored is None and self.conn.execute("SELECT 1 FROM episode_messages LIMIT 1").fetchone():
            stored = "assistant"  # created before lanes recorded their role: the assistant lane
            self._set_state("role", stored)
        if stored is None:
            self._set_state("role", role)
        elif stored != role:
            self.conn.close()
            raise RuntimeError(f"episodes DB holds the {stored} lane, not {role}")

    # ------------------------------------------------------------ plumbing

    def embedder(self):
        if self._embedder_factory is not None:
            return self._embedder_factory()
        if self._embedder is None:
            from .cpu import embed_threads
            from .embed.backends import get_embedder
            self._embedder = get_embedder(self.model, threads=embed_threads())
        return self._embedder

    def _get_state(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM episode_state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def _set_state(self, key: str, value) -> None:
        self.conn.execute(
            "INSERT INTO episode_state(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET"
            " value = excluded.value, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
            (key, str(value)))

    def set_state(self, key: str, value) -> None:
        with self.lock:
            self._set_state(key, value)

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    # ------------------------------------------------------------ ingest

    def ingest(self, state_db: str | Path) -> dict:
        """Copy new messages of this lane's role (id > watermark) from Hermes state.db, read-only."""
        p = Path(state_db).resolve()
        if not p.is_file():
            raise FileNotFoundError("state_db_missing")
        key = f"watermark:{p}"
        with self.lock:
            last = int(self._get_state(key) or 0)
        start = last
        stats = {"scanned": 0, "added": 0, "skipped": 0, "duplicates": 0, "injected": 0,
                 "redactions": 0, "chunks": 0}
        src = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5.0)
        try:
            injected = injected_sql(src) if self.role == "owner" else None
            while True:
                page_start = last
                rows = src.execute(
                    "SELECT m.id, m.session_id, m.role, m.content, m.timestamp, m.active,"
                    " m.compacted, s.source FROM messages m LEFT JOIN sessions s ON s.id = m.session_id"
                    " WHERE m.id > ?" + (not_injected_sql(src) if self.role == "owner" else "")
                    + " ORDER BY m.id LIMIT ?", (last, SCAN_PAGE)).fetchall()
                if not rows:
                    break
                keep = []
                for mid, sid, role, content, ts, active, compacted, source in rows:
                    if role != MESSAGE_ROLE[self.role] or source not in ALLOWED_SOURCES or not (active or compacted):
                        continue
                    text, hits = (clean_assistant(content) if self.role == "assistant"
                                  else clean_user(content, source))
                    if not text:
                        stats["skipped"] += 1
                        continue
                    keep.append((str(sid), int(mid), float(ts or 0.0), text, hits))
                last = int(rows[-1][0])
                stats["scanned"] += len(rows)
                if injected is not None:  # Hermes-injected turns, filtered out by the query above
                    stats["injected"] += src.execute(
                        "SELECT count(*) FROM messages m WHERE m.id > ? AND m.id <= ? AND m.role = 'user'"
                        + injected, (page_start, last)).fetchone()[0]
                with self.lock:
                    self.conn.execute("BEGIN IMMEDIATE")
                    try:
                        for sid, mid, ts, text, hits in keep:
                            sha = _text_sha(text)
                            if self.conn.execute("SELECT 1 FROM episode_messages WHERE text_sha = ? LIMIT 1",
                                                 (sha,)).fetchone():
                                stats["duplicates"] += 1  # a compaction copy of a stored message
                                continue
                            cur = self.conn.execute(
                                "INSERT OR IGNORE INTO episode_messages(session_id, message_id, ts, text,"
                                " redactions, text_sha) VALUES (?, ?, ?, ?, ?, ?)", (sid, mid, ts, text, hits, sha))
                            if not cur.rowcount:
                                continue
                            ref = cur.lastrowid
                            for seq, piece in enumerate(chunk_text(text)):
                                self.conn.execute("INSERT INTO episode_chunks(message_ref, seq, text)"
                                                  " VALUES (?, ?, ?)", (ref, seq, piece))
                                stats["chunks"] += 1
                            stats["added"] += 1
                            stats["redactions"] += hits
                        self._set_state(key, last)
                        self.conn.execute("COMMIT")
                    except BaseException:
                        self.conn.execute("ROLLBACK")
                        raise
                if len(rows) < SCAN_PAGE:
                    break
        finally:
            src.close()
        with self.lock:
            for name in TOTAL_COUNTERS:
                if stats[name]:
                    self._set_state(f"total:{name}", int(self._get_state(f"total:{name}") or 0) + stats[name])
        stats["watermark"] = last
        stats["previous_watermark"] = start
        return stats

    # ------------------------------------------------------------ embed

    def _min_ts(self) -> float:
        if self.max_age_days is None:
            return float("-inf")
        return time.time() - self.max_age_days * 86400

    _PENDING = (" FROM episode_chunks c JOIN episode_messages m ON m.id = c.message_ref"
                " WHERE (c.model IS NOT ? OR c.vector IS NULL) AND m.ts >= ?")

    def pending(self) -> int:
        """Chunks still to embed (within `max_age_days`, when set)."""
        with self.lock:
            return self.conn.execute("SELECT count(*)" + self._PENDING,
                                     (self.model, self._min_ts())).fetchone()[0]

    def embed(self, limit: int = EMBED_BATCH, *, max_seconds: float | None = None,
              yield_to=None) -> int:
        """Embed up to `limit` chunks lacking a vector, newest replies first; returns the count.

        Newest first, so recent conversations become searchable within the first ticks
        of a large backfill instead of after the whole history.
        """
        done = 0
        started = time.monotonic()
        while done < limit:
            if max_seconds is not None and time.monotonic() - started >= max_seconds:
                break
            if LOCK_WANTED.is_set():
                break  # the review assist waits for TICK_LOCK: resume next tick
            if yield_to is not None and not _wait_quiet(lambda: yield_to() and not LOCK_WANTED.is_set()):
                break  # live traffic kept the sidecar busy: resume next tick
            if LOCK_WANTED.is_set():
                break
            n = min(EMBED_SUB_BATCH, limit - done)
            with self.lock:
                rows = self.conn.execute(
                    "SELECT c.id, c.text" + self._PENDING + " ORDER BY m.ts DESC, c.id LIMIT ?",
                    (self.model, self._min_ts(), n)).fetchall()
            if not rows:
                break
            vecs = np.asarray(self.embedder().embed_passages([r["text"] for r in rows]), dtype=np.float32)
            with self.lock:
                self.conn.execute("BEGIN IMMEDIATE")
                try:
                    for r, v in zip(rows, vecs):
                        self.conn.execute(
                            "UPDATE episode_chunks SET model = ?, dim = ?, vector = ? WHERE id = ?",
                            (self.model, int(v.shape[0]), v.tobytes(), r["id"]))
                    self.conn.execute("COMMIT")
                except BaseException:
                    self.conn.execute("ROLLBACK")
                    raise
                self._gen += 1
            done += len(rows)
        return done

    # ------------------------------------------------------------ search

    def _load_matrix(self):
        """(chunk_ids, mat) of all vectors for `self.model`; cached until rows change."""
        with self.lock:
            version = self.conn.execute("PRAGMA data_version").fetchone()[0]
            key = (self.model, self._gen, version)
            if self._matrix is not None and self._matrix[0] == key:
                return self._matrix[1], self._matrix[2]
            rows = self.conn.execute(
                "SELECT id, dim, vector FROM episode_chunks WHERE model = ? AND vector IS NOT NULL"
                " ORDER BY id", (self.model,)).fetchall()
            dims = {r["dim"] for r in rows}
            if len(dims) > 1:
                raise RuntimeError("episodes vectors have mixed dimensions")
            ids = np.array([r["id"] for r in rows], dtype=np.int64)
            mat = (np.stack([np.frombuffer(r["vector"], dtype=np.float32) for r in rows])
                   if rows else np.zeros((0, 0), dtype=np.float32))
            self._matrix = (key, ids, mat)
            return ids, mat

    def search(self, query, k: int = 5, *, min_score: float | None = None,
               exclude_session: str | None = None, query_vector=None) -> dict:
        """Top-k messages of this lane by cosine; one hit per message (its best chunk)."""
        if type(k) is not int or not 1 <= k <= MAX_K:
            raise ValueError("episodes k must be between 1 and 20")
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= MAX_QUERY:
            raise ValueError("episodes query must contain 1..2048 characters")
        origin, authority, answer_use = LANES[self.role]
        ids, mat = self._load_matrix()
        hits: list[dict] = []
        if ids.size:
            q = (np.asarray(query_vector, dtype=np.float32) if query_vector is not None
                 else np.asarray(self._query_vector(query) if self._query_vector is not None
                                 else self.embedder().embed_query(query), dtype=np.float32))
            sims = mat @ q
            order = np.lexsort((ids, -sims))
            seen: set[int] = set()
            with self.lock:
                for i in order:
                    score = float(sims[i])
                    if min_score is not None and score < min_score:
                        break
                    row = self.conn.execute(
                        "SELECT c.text, c.message_ref, m.session_id, m.message_id, m.ts FROM episode_chunks c"
                        " JOIN episode_messages m ON m.id = c.message_ref WHERE c.id = ?",
                        (int(ids[i]),)).fetchone()
                    if row is None or row["message_ref"] in seen:
                        continue
                    if exclude_session and row["session_id"] == exclude_session:
                        continue
                    seen.add(row["message_ref"])
                    hits.append({
                        "origin": origin, "authority": authority,
                        "locator": f"hermes:{row['session_id']}:{row['message_id']}",
                        "session_id": row["session_id"], "message_id": row["message_id"],
                        "timestamp": _iso(row["ts"]), "score": round(score, 4), "text": row["text"]})
                    if len(hits) >= k:
                        break
        return {"origin": origin, "answer_use": answer_use, "model": self.model, "hits": hits}

    # ------------------------------------------------------------ status

    def stats(self) -> dict:
        with self.lock:
            c = self.conn
            messages = c.execute("SELECT count(*) FROM episode_messages").fetchone()[0]
            chunks = c.execute("SELECT count(*) FROM episode_chunks").fetchone()[0]
            embedded = c.execute("SELECT count(*) FROM episode_chunks WHERE model = ? AND vector IS NOT"
                                 " NULL", (self.model,)).fetchone()[0]
            span = c.execute("SELECT min(ts), max(ts) FROM episode_messages").fetchone()
            state = {r["key"]: r["value"] for r in c.execute("SELECT key, value FROM episode_state")}
        return {"role": self.role, "model": self.model, "messages": messages, "chunks": chunks, "embedded": embedded,
                "pending": chunks - embedded,
                "oldest": _iso(span[0]) if span[0] is not None else None,
                "newest": _iso(span[1]) if span[1] is not None else None,
                "watermarks": {k.split(":", 1)[1]: int(v) for k, v in state.items()
                               if k.startswith("watermark:")},
                "ingest_totals": {name: int(state.get(f"total:{name}") or 0) for name in TOTAL_COUNTERS},
                "last_tick_at": state.get("last_tick_at"), "last_error": state.get("last_error") or None}

    # ------------------------------------------------------------ background

    def tick(self, state_db: str | Path | None, limit: int = EMBED_BATCH, *,
             max_seconds: float | None = EMBED_TICK_SECONDS, yield_to=None) -> dict:
        """One background step: ingest new messages, embed at most `limit` chunks.

        Records `last_tick_at` (heartbeat) and `last_error` (a stable code, no content).
        """
        out: dict = {}
        error = ""
        started = time.monotonic()
        try:
            if state_db:
                out["ingest"] = self.ingest(state_db)
            out["embedded"] = self.embed(limit, max_seconds=max_seconds, yield_to=yield_to)
        except FileNotFoundError:
            error = "state_db_missing"
        except Exception as exc:  # never content or paths in the heartbeat
            error = type(exc).__name__
            log.warning("episodes tick failed: %s", error)
        self.set_state("last_tick_at", _now())
        self.set_state("last_error", error)
        out["error"] = error or None
        ingest = out.get("ingest") or {}
        log.info("episodes tick role=%s added=%d embedded=%d pending=%d seconds=%.1f error=%s", self.role,
                 ingest.get("added", 0), out.get("embedded") or 0, self.pending(), time.monotonic() - started,
                 error or "-")
        return out


def _wait_quiet(busy, max_wait: float = YIELD_WAIT_SECONDS, poll: float = 0.5) -> bool:
    """Wait until `busy()` is False; False when it stayed busy for `max_wait` seconds."""
    deadline = time.monotonic() + max_wait
    while busy():
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)
    return True


class EpisodeWorker:
    """Daemon thread: `Episodes.tick` every `interval` seconds until stopped."""

    def __init__(self, episodes: Episodes, state_db, interval: float = 300.0,
                 limit: int = WORKER_TICK_LIMIT, yield_to=None, tick_seconds: float = EMBED_TICK_SECONDS):
        self.episodes = episodes
        self.tick_seconds = tick_seconds
        self.yield_to = yield_to  # callable: True while live queries need the CPU
        self.state_db = state_db
        self.interval = interval
        self.limit = limit
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="quoth-episodes", daemon=True)

    def start(self) -> "EpisodeWorker":
        self.thread.start()
        return self

    def _run(self) -> None:
        while not self.stop_event.is_set():
            started = time.monotonic()
            with TICK_LOCK:  # the other lane's tick finishes first; both get the whole CPU in turn
                if self.stop_event.is_set():
                    break
                self.episodes.tick(self.state_db, self.limit, max_seconds=self.tick_seconds,
                                   yield_to=self.yield_to)
            self.stop_event.wait(max(1.0, self.interval - (time.monotonic() - started)))

    def stop(self, timeout: float = 5.0) -> None:
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout)


def health(stats: dict, interval: float) -> dict:
    """green/red for the lane: red when the last tick errored or is older than 3 intervals."""
    reasons = []
    last = stats.get("last_tick_at")
    if last is None:
        reasons.append("never_ticked")
    else:
        age = (datetime.now(tz=timezone.utc) - datetime.fromisoformat(last)).total_seconds()
        if age > 3 * interval:
            reasons.append("stale")
    if stats.get("last_error"):
        reasons.append(f"error:{stats['last_error']}")
    return {"status": "red" if reasons else "green", "reasons": reasons,
            **{k: stats[k] for k in ("messages", "embedded", "pending", "newest", "last_tick_at")}}


# ---------------------------------------------------------------- CLI

def main(argv=None) -> int:
    """`quoth episodes ingest|search|stats|health`."""
    import argparse
    import json

    p = argparse.ArgumentParser(prog="quoth episodes",
                                description="Conversation lanes: what the assistant said, or what"
                                            " the owner wrote, searchable by meaning.")
    p.add_argument("--episodes", type=Path, default=None,
                   help="lane DB (default $QUOTH_HOME/episodes.db, or $QUOTH_HOME/owner-episodes.db"
                        " with --role owner)")
    p.add_argument("--role", choices=sorted(LANES), default="assistant",
                   help="assistant: what the assistant said; owner: what the owner wrote")
    p.add_argument("--model", default="bge-m3")
    p.add_argument("--max-age-days", type=float, default=None,
                   help="embed only replies newer than this many days (default: all, newest first)")
    sub = p.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("ingest", help="copy the lane's new messages from state.db and embed them")
    pi.add_argument("--state-db", type=Path, default=None,
                    help="Hermes state.db (default $HERMES_HOME/state.db)")
    pi.add_argument("--max-chunks", type=int, default=None,
                    help="embed at most N chunks (default: all pending; 0 = ingest only)")
    ps = sub.add_parser("search", help="search the lane by meaning")
    ps.add_argument("query")
    ps.add_argument("-k", "--top-k", dest="k", type=int, default=5, help="hits to return (1-20)")
    sub.add_parser("stats", help="counts, watermark, last background tick")
    ph = sub.add_parser("health", help="exit 1 when the background tick is stale or failing")
    ph.add_argument("--interval", type=float, default=300.0, help="the sidecar's --episodes-interval")
    args = p.parse_args(argv)

    eps = Episodes(args.episodes, model=args.model, max_age_days=args.max_age_days, role=args.role)
    try:
        if args.cmd == "ingest":
            out = {"ingest": eps.ingest(args.state_db or paths.hermes_state_db()), "embedded": 0}
            remaining = args.max_chunks
            while remaining is None or remaining > 0:
                step = EMBED_BATCH if remaining is None else min(EMBED_BATCH, remaining)
                n = eps.embed(step)
                out["embedded"] += n
                if remaining is not None:
                    remaining -= n
                if n < step:
                    break
            out["pending"] = eps.pending()
        elif args.cmd == "search":
            out = eps.search(args.query, args.k)
        elif args.cmd == "health":
            out = health(eps.stats(), args.interval)
        else:
            out = eps.stats()
    except (ValueError, FileNotFoundError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2
    finally:
        eps.close()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if args.cmd == "health" and out["status"] == "red" else 0


if __name__ == "__main__":
    sys.exit(main())
