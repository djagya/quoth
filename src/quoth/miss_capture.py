"""Opt-in miss capture: the owner's question text of likely recall misses, for review.

Off unless the sidecar gets ``--capture-misses DIR``. Unlike the prefetch log (hashes,
ids, locators, never text), this keeps the owner's question text, so it is narrow by design:

- Only an authored live turn followed, in the same session, by the assistant's own memory
  search (an ``evidence_bundle`` query, not a path fetch) within ``window`` seconds and
  before that session's next prefetch becomes a candidate. Nothing is captured otherwise.
- ``signal`` says what the turn did: ``searched_after_empty`` (no vault evidence injected:
  empty pool, or late/busy/unavailable vault), ``gate_rejected_pool`` (no vault evidence
  injected though the pool had candidates: every one failed the relevance gate) or
  ``searched_despite_injection`` (evidence was injected and the assistant still searched:
  possibly the wrong notes). Injected claims and lane excerpts are noted separately.
- A candidate holds the question (the authored ``prefetch_query`` text, already capped), the
  injected locators and paths, the assistant's search queries and the top paths each
  returned (candidate gold for the reviewer), the snapshot id and the quoth version. Never
  the assistant's reply and never excerpt text.

Candidates live in one file, ``DIR/misses.jsonl`` (mode 0600, directory 0700, wider modes are
repaired at start), on the host only: nothing here sends them anywhere. Identical questions
are merged, at most ``max_entries`` are kept, and candidates older than ``days`` are pruned.
``quoth eval review DIR`` turns them into eval rows or deletes them.

Correlation state is a bounded per-session map in memory; the request path only updates it
and queues a record. One background thread does the file work under an advisory lock (shared
with ``eval review``), so a full queue drops the record (counted) and an I/O error is counted
and logged once: a turn is never slowed or broken by the capture.
"""
from __future__ import annotations

from collections import OrderedDict, Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import queue
import stat
import threading
import time
import uuid

from .recall_defaults import MISS_CAPTURE_DAYS, MISS_CAPTURE_MAX, MISS_CAPTURE_WINDOW, QUERY_MAX_CHARS

log = logging.getLogger(__name__)

SCHEMA = "quoth-miss-queue/1"
QUEUE_FILE = "misses.jsonl"
LOCK_FILE = "misses.lock"
SIGNALS = ("searched_after_empty", "gate_rejected_pool", "searched_despite_injection")
DEFAULT_WINDOW = MISS_CAPTURE_WINDOW
DEFAULT_DAYS = MISS_CAPTURE_DAYS
DEFAULT_MAX = MISS_CAPTURE_MAX
MAX_SESSIONS = 256    # sessions whose last turn is tracked at once (least recent go first)
MAX_SEARCHES = 3      # searches recorded per turn (and kept per merged candidate)
TOP_PATHS = 5         # paths kept per search
QUEUE_MAX = 200
PRUNE_EVERY = 3600.0  # seconds between retention passes with no new candidate
_FLUSH = "__flush__"


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="milliseconds")


def question_key(text: str) -> str:
    """Dedupe key: sha256 of the case-folded, whitespace-collapsed question."""
    return hashlib.sha256(" ".join(str(text).casefold().split()).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- private files

def private_dir(directory: str | Path) -> Path:
    """Create the capture directory 0700, or repair a wider mode; refuse a symlink or a
    non-directory (the question text must not land somewhere else)."""
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise ValueError("miss capture directory must be a real directory")
    if stat.S_IMODE(st.st_mode) & 0o077:
        os.chmod(path, 0o700)
    queue_path = path / QUEUE_FILE
    try:
        qst = os.lstat(queue_path)
    except FileNotFoundError:
        return path
    if not stat.S_ISREG(qst.st_mode):
        raise ValueError("miss capture queue must be a regular file")
    if stat.S_IMODE(qst.st_mode) & 0o077:
        os.chmod(queue_path, 0o600)
    return path


def queue_path(target: str | Path) -> tuple[Path, Path]:
    """(directory, queue file) of a capture directory or of its queue file."""
    target = Path(target)
    if target.is_dir() or not target.suffix:
        return target, target / QUEUE_FILE
    return target.parent, target


@contextmanager
def locked(directory: Path):
    """Exclusive advisory lock over the queue (the sidecar writer and `eval review`)."""
    fd = os.open(directory / LOCK_FILE, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing releases the lock


def read_queue(path: Path) -> list[dict]:
    """The queue's candidates; unreadable lines are skipped (never fatal)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return []
    with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as fh:
        raw = fh.read()
    out = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("id"), str) and isinstance(entry.get("question"), str):
            out.append(entry)
    return out


def write_queue(path: Path, entries: list[dict], *, sync: bool = False) -> None:
    """Replace the queue atomically with a 0600 file (a removed candidate's text is gone)."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        data = "".join(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n" for e in entries)
        os.write(fd, data.encode("utf-8"))
        if sync:
            os.fsync(fd)
    except BaseException:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    os.close(fd)
    os.replace(tmp, path)


def _when(ts) -> datetime | None:
    try:
        out = datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    return out if out.tzinfo else out.replace(tzinfo=timezone.utc)


def prune(entries: list[dict], *, days: int = DEFAULT_DAYS, max_entries: int = DEFAULT_MAX,
          now: datetime | None = None) -> list[dict]:
    """Drop candidates last seen more than `days` ago, then keep the newest `max_entries`."""
    cutoff = (now or datetime.now(tz=timezone.utc)) - timedelta(days=days)
    kept = [e for e in entries if (_when(e.get("last_ts") or e.get("ts")) or cutoff) > cutoff]
    kept.sort(key=lambda e: _when(e.get("last_ts") or e.get("ts")) or cutoff)
    return kept[-max_entries:] if max_entries > 0 else []


def merge(entries: list[dict], record: dict) -> list[dict]:
    """Add a candidate, or fold it into the pending one with the same question."""
    key = record.get("key") or question_key(record["question"])
    for entry in entries:
        if entry.get("key") == key:
            entry["seen"] = int(entry.get("seen", 1)) + 1
            entry["last_ts"] = record.get("ts") or entry.get("last_ts")
            if record.get("signal") not in entry.setdefault("signals", [entry.get("signal")]):
                entry["signals"].append(record.get("signal"))
            known = {s.get("query") for s in entry.get("searches", [])}
            for search in record.get("searches", []):
                if search.get("query") not in known and len(entry.setdefault("searches", [])) < MAX_SEARCHES:
                    entry["searches"].append(search)
                    known.add(search.get("query"))
            for name in ("injected", "injected_paths"):
                for item in record.get(name, []):
                    if item not in entry.setdefault(name, []):
                        entry[name].append(item)
            return entries
    entries.append({**record, "key": key})
    return entries


def stats(entries: list[dict]) -> dict:
    """Counts only: no question, query or path."""
    stamps = sorted(e.get("ts") for e in entries if isinstance(e.get("ts"), str))
    return {"pending": len(entries), "by_signal": dict(sorted(Counter(e.get("signal") for e in entries).items())),
            "merged_repeats": sum(max(0, int(e.get("seen", 1)) - 1) for e in entries),
            "oldest": stamps[0] if stamps else None, "newest": stamps[-1] if stamps else None}


def _paths(hits) -> list[str]:
    out = []
    for h in hits or []:
        p = h.get("path") or (h.get("locator") or "").rsplit(":", 1)[0] or None
        if p and p not in out:
            out.append(p)
        if len(out) >= TOP_PATHS:
            break
    return out


# ---------------------------------------------------------------- the capture

class MissCapture:
    """Per-session correlation of turns and searches; candidates written off the request path."""

    def __init__(self, directory: str | Path, *, window: float = DEFAULT_WINDOW, days: int = DEFAULT_DAYS,
                 max_entries: int = DEFAULT_MAX, max_sessions: int = MAX_SESSIONS, max_queue: int = QUEUE_MAX,
                 prune_every: float = PRUNE_EVERY):
        from . import __version__
        self.version = __version__
        self.dir = private_dir(directory)
        self.path = self.dir / QUEUE_FILE
        self.window = float(window)
        self.days = max(1, int(days))
        self.max_entries = max(1, int(max_entries))
        self.max_sessions = max(1, int(max_sessions))
        self.prune_every = float(prune_every)
        self.dropped = 0
        self.errors = 0
        self.written = 0
        self._lock = threading.Lock()
        self._sessions: OrderedDict[str, dict] = OrderedDict()
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue)
        self._closed = False
        self._queue.put_nowait("prune")  # retention runs once at start
        self._thread = threading.Thread(target=self._run, name="quoth-miss-capture", daemon=True)
        self._thread.start()

    # -- request path: dict updates under a lock, never raises

    def turn(self, session_id: str, question: str, *, injected: list[tuple[str, str | None]] = (),
             claims: list = (), lanes: dict | None = None, vault_status: str | None = None,
             pool_n: int = 0, passed_n: int = 0, snapshot_id: str | None = None) -> None:
        """An authored live turn returned: it opens this session's window (and closes the last)."""
        try:
            if not session_id:
                return
            entry = {"t": time.monotonic(), "ts": now_iso(), "question": str(question)[:QUERY_MAX_CHARS],
                     "injected": [loc for loc, _ in injected if loc],
                     "injected_paths": list(dict.fromkeys(p for _, p in injected if p)),
                     "claims": list(claims), "lanes": dict(lanes or {}), "vault_status": vault_status,
                     "pool_n": int(pool_n), "passed_n": int(passed_n), "snapshot_id": snapshot_id,
                     "searches": 0}
            with self._lock:
                self._sessions[session_id] = entry
                self._sessions.move_to_end(session_id)
                while len(self._sessions) > self.max_sessions:
                    self._sessions.popitem(last=False)
        except Exception:
            self._error()

    def close_turn(self, session_id: str) -> None:
        """A turn nobody authored (kanban wake, cron delivery) ends the session's window."""
        try:
            with self._lock:
                self._sessions.pop(session_id, None)
        except Exception:
            self._error()

    def search(self, session_id: str, query: str, hits, snapshot_id: str | None = None) -> None:
        """The assistant searched memory: a likely miss when the session's turn window is open."""
        try:
            if not session_id or not query:
                return
            now = time.monotonic()
            with self._lock:
                turn = self._sessions.get(session_id)
                if turn is None:
                    return
                if now - turn["t"] > self.window:
                    del self._sessions[session_id]
                    return
                if turn["searches"] >= MAX_SEARCHES:
                    return
                turn["searches"] += 1
                turn = dict(turn)
            if turn["injected"]:
                signal = "searched_despite_injection"
            elif turn["pool_n"] and turn["vault_status"] == "ok":
                signal = "gate_rejected_pool"
            else:
                signal = "searched_after_empty"
            record = {"schema": SCHEMA, "id": uuid.uuid4().hex[:16], "ts": turn["ts"], "last_ts": now_iso(),
                      "signal": signal, "question": turn["question"],
                      "injected": turn["injected"], "injected_paths": turn["injected_paths"],
                      "claims_injected": turn["claims"], "lanes_injected": turn["lanes"],
                      "vault_status": turn["vault_status"], "pool_n": turn["pool_n"], "passed_n": turn["passed_n"],
                      "searches": [{"query": str(query)[:QUERY_MAX_CHARS], "paths": _paths(hits),
                                    "after_s": round(now - turn["t"], 1)}],
                      "snapshot_id": turn["snapshot_id"] or snapshot_id, "v": self.version, "seen": 1}
            self._put(record)
        except Exception:
            self._error()

    def _put(self, item) -> None:
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            self.dropped += 1

    def counters(self) -> dict:
        with self._lock:
            tracked = len(self._sessions)
        return {"dropped": self.dropped, "errors": self.errors, "written": self.written, "tracked": tracked,
                "queued": self._queue.qsize()}

    def flush(self, timeout: float = 5.0) -> None:
        done = threading.Event()
        try:
            self._queue.put((_FLUSH, done), timeout=timeout)
        except queue.Full:
            return
        done.wait(timeout)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._queue.put(None, timeout=1.0)
        except queue.Full:
            pass
        self._thread.join(2.0)

    # -- writer thread

    def _error(self) -> None:
        self.errors += 1
        if self.errors == 1:
            log.warning("miss capture failed; candidates are dropped until it recovers")

    def _apply(self, record: dict | None) -> None:
        try:
            with locked(self.dir):
                entries = read_queue(self.path)
                if record is not None:
                    entries = merge(entries, record)
                write_queue(self.path, prune(entries, days=self.days, max_entries=self.max_entries))
            if record is not None:
                self.written += 1
        except Exception:
            self._error()

    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=self.prune_every)
            except queue.Empty:
                item = "prune"
            if item is None:
                break
            if isinstance(item, tuple) and item and item[0] == _FLUSH:
                item[1].set()
                continue
            if item == "prune":
                if self.path.exists():
                    self._apply(None)
                continue
            self._apply(item)
