"""Privacy-safe prefetch log: JSON lines per recall turn, for online measurement.

Off unless the sidecar gets ``--prefetch-log DIR``. Lines hold hashes, ids, locators,
scores, statuses, config and timings, never the query or any excerpt text. Every line
carries ``inst`` (this process instance) and ``ts`` (UTC, for joining across files); the
``*_ms`` offsets of one request are monotonic durations from its start, in one process.

One authored prefetch turn (``req``) writes, in order of the events (the lines may not be
in this order: join by id, never by position):

- ``start``: before anything is submitted: code and config identity, budgets, gate.
- ``submitted`` (its evidence job ``job`` was started) or ``busy`` (no job was started:
  the previous one, ``busy_job``, still runs).
- ``wait``: the turn began waiting for the job; ``deadline_ms`` is the effective deadline.
- ``prefetch``: the turn returned (status, candidates, what was injected, timings).
- ``done``: the job finished: ``normal`` (raw count, candidate locators, the post-gate
  would-inject locators or ``unknown``), ``exception`` (type name only) or ``censored``
  (still running when the sidecar closed). Exactly one per job; it may come before
  ``prefetch``.

Plus ``instance`` (once per process), ``counters`` (every ``counters_every`` seconds and at
close: dropped and failed lines, jobs in flight, completions with no returned turn), and
``tool`` / ``skip`` records (search tool calls, skipped injected turns).

Locators can contain vault paths, so the log is private and stays on the host. Writes go
through a bounded queue to one background thread: a full queue drops the record (counted),
an I/O error is counted and logged once, and nothing is fsynced, so a turn is never slowed
or broken. A record may be a zero-argument callable, built on the writer thread (the
counterfactual gate decision is computed there, off the request and the evidence worker).
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
import sys
import threading
import time
import uuid

from .recall_defaults import PREFETCH_LOG_DAYS

log = logging.getLogger(__name__)

SCHEMA = "quoth-prefetch-log/2"
FILE_RE = re.compile(r"^prefetch-(\d{4}-\d{2}-\d{2})\.jsonl$")
DEFAULT_DAYS = PREFETCH_LOG_DAYS
QUEUE_MAX = 2000
COUNTERS_EVERY = 300.0  # seconds between `counters` lines
MAX_TRACKED = 1024      # jobs tracked at once (in flight, or done with no returned turn)
_FLUSH = "__flush__"


def session_hash(session_id: str | None) -> str | None:
    """Truncated sha256 of a session id: joins turns to tool calls without storing the id."""
    if not session_id:
        return None
    return hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:16]


def query_digest(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def config_digest(config: dict) -> str:
    """Short sha256 of a config dict (canonical JSON): one value per serving configuration."""
    raw = json.dumps(config, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat(timespec="milliseconds")


def new_id() -> str:
    return uuid.uuid4().hex[:16]


_CODE = None


def code_digest() -> str:
    """sha256 over the loaded quoth package's Python sources (relative path + bytes): which
    code is running, apart from the version label. Computed once per process."""
    global _CODE
    if _CODE is None:
        root = Path(__file__).parent
        h = hashlib.sha256()
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            h.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
            try:
                h.update(path.read_bytes())
            except OSError:
                h.update(b"?")
        _CODE = h.hexdigest()[:16]
    return _CODE


def file_day(name: str):
    m = FILE_RE.match(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


class PrefetchLog:
    """Append-only daily JSONL files written by a daemon thread; `write` never blocks.

    It also tracks the evidence jobs of logged turns (`job_started`, `job_returned`,
    `job_completed`), so a job gets exactly one `done` line and the `counters` lines can
    say how many are in flight or were never joined to a returned turn.
    """

    def __init__(self, directory: str | Path, days: int = DEFAULT_DAYS, *, max_queue: int = QUEUE_MAX,
                 counters_every: float = COUNTERS_EVERY):
        from . import __version__
        self.dir = Path(directory)
        self.days = max(1, int(days))
        self.inst = new_id()
        self.counters_every = float(counters_every)
        self.dropped = 0
        self.errors = 0
        self.written = 0
        self._jobs_lock = threading.Lock()
        self._jobs: dict[str, dict] = {}  # job id -> {"req", "returned", "completed"}
        self.jobs_started = 0
        self.jobs_completed = 0
        self.jobs_censored = 0
        self.completed_before_return = 0
        self.untracked = 0  # jobs evicted from tracking (MAX_TRACKED reached)
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue)
        self._day = None
        self._fd = None
        self._closed = False
        self._queue.put_nowait({"type": "instance", "ts": now_iso(), "v": __version__,
                                "code_sha256": code_digest(), "pid": os.getpid(),
                                "python": "%d.%d.%d" % sys.version_info[:3], "schema": SCHEMA})
        self._thread = threading.Thread(target=self._run, name="quoth-prefetch-log", daemon=True)
        self._thread.start()

    def write(self, record) -> None:
        """Queue a record (a dict, or a zero-argument callable returning one); drop when full."""
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            self.dropped += 1

    def flush(self, timeout: float = 5.0) -> None:
        """Wait until queued records are written (tests and shutdown)."""
        done = threading.Event()
        try:
            self._queue.put((_FLUSH, done), timeout=timeout)
        except queue.Full:
            return
        done.wait(timeout)

    def close(self) -> None:
        """Write the queue out, then a final `counters` line, and stop the writer."""
        if self._closed:
            return
        self._closed = True
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

    # ------------------------------------------------------------ job tracking

    def job_started(self, job: str, req: str) -> None:
        with self._jobs_lock:
            self.jobs_started += 1
            if len(self._jobs) >= MAX_TRACKED:  # bounded: the oldest entry stops being tracked
                self._jobs.pop(next(iter(self._jobs)))
                self.untracked += 1
            self._jobs[job] = {"req": req, "returned": False, "completed": False}

    def job_returned(self, job: str | None) -> None:
        """The turn that started `job` returned (its `prefetch` line was queued)."""
        if job is None:
            return
        with self._jobs_lock:
            entry = self._jobs.get(job)
            if entry is None:
                return
            entry["returned"] = True
            if entry["completed"]:
                del self._jobs[job]

    def job_completed(self, job: str, censored: bool = False) -> bool:
        """Mark `job` done; False when it already was (its `done` line is written once)."""
        with self._jobs_lock:
            entry = self._jobs.get(job)
            if entry is None or entry["completed"]:
                return False
            entry["completed"] = True
            if censored:
                self.jobs_censored += 1
            else:
                self.jobs_completed += 1
            if entry["returned"]:
                del self._jobs[job]
            else:
                self.completed_before_return += 1
        return True

    def in_flight(self) -> list[tuple[str, str]]:
        """(job, req) of the tracked jobs not yet done."""
        with self._jobs_lock:
            return [(job, e["req"]) for job, e in self._jobs.items() if not e["completed"]]

    def counters(self) -> dict:
        with self._jobs_lock:
            in_flight = sum(not e["completed"] for e in self._jobs.values())
            unjoined = sum(e["completed"] and not e["returned"] for e in self._jobs.values())
            jobs = {"jobs_started": self.jobs_started, "jobs_completed": self.jobs_completed,
                    "jobs_censored": self.jobs_censored, "in_flight": in_flight,
                    "unjoined_completions": unjoined,
                    "completed_before_return": self.completed_before_return,
                    "untracked": self.untracked}
        return {"dropped": self.dropped, "errors": self.errors, "written": self.written,
                "queued": self._queue.qsize(), **jobs}

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

    def _emit(self, item) -> None:
        try:
            if callable(item):
                item = item()
                if item is None:
                    return
            item.setdefault("inst", self.inst)
            day = datetime.now(tz=timezone.utc).date()
            if day != self._day or self._fd is None:
                self._open(day)
            line = json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
            os.write(self._fd, line.encode("utf-8"))
            self.written += 1
        except Exception:
            self._error()
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None

    def _counters_line(self, reason: str) -> None:
        # Written by this thread directly, never queued: a full queue cannot drop it.
        self._emit({"type": "counters", "ts": now_iso(), "reason": reason, **self.counters()})

    def _run(self) -> None:
        next_counters = time.monotonic() + self.counters_every
        while True:
            try:
                item = self._queue.get(timeout=max(0.0, next_counters - time.monotonic()))
            except queue.Empty:
                item = _FLUSH  # the periodic counters are due
            if time.monotonic() >= next_counters:
                self._counters_line("periodic")
                next_counters = time.monotonic() + self.counters_every
            if item is None:
                break
            if item == _FLUSH:
                continue
            if isinstance(item, tuple) and item and item[0] == _FLUSH:
                item[1].set()
                continue
            self._emit(item)
        self._counters_line("close")
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
