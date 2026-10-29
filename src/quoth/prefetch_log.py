"""Privacy-safe prefetch log: one JSON line per recall turn, for online measurement.

Off unless the sidecar gets ``--prefetch-log DIR``. Each line holds hashes, ids,
locators, scores, statuses and timings, never the query or any excerpt text:

- ``{"type": "prefetch", "ts", "v", "session", "query_sha256", "query_len",
  "stores": {store: [{"id"|"locator", "score", "pool", "passed", "gate", "injected", ...}]},
  "status", "ms"}``: per candidate, ``pool`` (the turn judged it for injection), ``passed``
  (it cleared the relevance gate) and ``injected`` (it reached the recall block)
- ``{"type": "tool", "ts", "v", "tool", "session", "k", "results": [locator, ...]}``

Locators can contain vault paths, so the log is private and stays on the host. Writes go
through a bounded queue to one background thread: a full queue drops the record, an I/O
error is logged once, and nothing is fsynced, so a turn is never slowed or broken.
Files are ``prefetch-YYYY-MM-DD.jsonl`` (UTC); files older than ``days`` are pruned.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import queue
import re
import threading

from .recall_defaults import PREFETCH_LOG_DAYS

log = logging.getLogger(__name__)

SCHEMA = "quoth-prefetch-log/1"
FILE_RE = re.compile(r"^prefetch-(\d{4}-\d{2}-\d{2})\.jsonl$")
DEFAULT_DAYS = PREFETCH_LOG_DAYS
QUEUE_MAX = 2000


def session_hash(session_id: str | None) -> str | None:
    """Truncated sha256 of a session id: joins turns to tool calls without storing the id."""
    if not session_id:
        return None
    return hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:16]


def query_digest(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="milliseconds")


def file_day(name: str):
    m = FILE_RE.match(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


class PrefetchLog:
    """Append-only daily JSONL files written by a daemon thread; `write` never blocks."""

    def __init__(self, directory: str | Path, days: int = DEFAULT_DAYS, *, max_queue: int = QUEUE_MAX):
        self.dir = Path(directory)
        self.days = max(1, int(days))
        self.dropped = 0
        self.errors = 0
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue)
        self._day = None
        self._fd = None
        self._thread = threading.Thread(target=self._run, name="quoth-prefetch-log", daemon=True)
        self._thread.start()

    def write(self, record: dict) -> None:
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            self.dropped += 1

    def flush(self, timeout: float = 5.0) -> None:
        """Wait until queued records are written (tests and shutdown)."""
        done = threading.Event()
        try:
            self._queue.put(("__flush__", done), timeout=timeout)
        except queue.Full:
            return
        done.wait(timeout)

    def close(self) -> None:
        try:
            self._queue.put(None, timeout=1.0)
        except queue.Full:
            pass
        self._thread.join(2.0)

    def prune(self, today=None) -> int:
        today = today or datetime.now(tz=timezone.utc).date()
        cutoff = today - timedelta(days=self.days)
        removed = 0
        try:
            for path in self.dir.iterdir():
                day = file_day(path.name)
                if day is not None and day < cutoff:
                    path.unlink(missing_ok=True)
                    removed += 1
        except OSError:
            self._error()
        return removed

    # ------------------------------------------------------------ writer thread

    def _error(self) -> None:
        self.errors += 1
        if self.errors == 1:
            log.warning("prefetch log write failed; records are dropped until it recovers")

    def _open(self, day) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.dir / f"prefetch-{day.isoformat()}.jsonl"
        self._fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        self._day = day
        self.prune(day)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                break
            if isinstance(item, tuple) and item and item[0] == "__flush__":
                item[1].set()
                continue
            try:
                day = datetime.now(tz=timezone.utc).date()
                if day != self._day or self._fd is None:
                    self._open(day)
                line = json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
                os.write(self._fd, line.encode("utf-8"))
            except Exception:
                self._error()
                if self._fd is not None:
                    try:
                        os.close(self._fd)
                    except OSError:
                        pass
                    self._fd = None
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
