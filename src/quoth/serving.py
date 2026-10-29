"""Atomic, fail-closed snapshot pointer admission and serialized connection leases.

A served pointer (a symlink naming a frozen snapshot) is admitted by a small state
machine that runs off the request path:

- **admitted**: the snapshot leases hand out. Leases never validate anything.
- **candidate**: the watcher thread saw the pointer name another target or file. It
  validates the candidate (manifest, sha256, ``integrity_check``) and warms it (the
  ``prepare`` hook: matrix, dates, a test query) while the admitted snapshot keeps
  serving, then swaps it in under the lease lock.
- **rejected**: validation or warmup failed. The admitted snapshot keeps serving, and the
  same (target, file identity) is not revalidated until the pointer or the file changes,
  or the retry delay passes (15, 30, then every 60 minutes).

A sidecar may start with no admitted snapshot (``require=False``): leases then raise
`SnapshotUnavailable` until the watcher admits one. Logs and status carry reason classes
and a digest of the target, never exception text or paths: manifests are untrusted.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import threading
import time
import uuid

from .db import connect_read_only, validate_evidence_db
from .refresh import sha

log = logging.getLogger(__name__)

WATCH_INTERVAL = 5.0
# Seconds before a rejected (target, file identity) is validated again; the last repeats.
REJECT_RETRY = (900.0, 1800.0, 3600.0)
# Health is red when a candidate has been pending this long (validation or warmup hung).
CANDIDATE_MAX_AGE = 3600.0
# Health is red when the watcher has not polled for this many intervals (at least 60 s).
WATCHER_STALE_INTERVALS = 10


class SnapshotUnavailable(RuntimeError):
    """No snapshot is admitted (a bad pointer at startup); the watcher may admit one later."""


def file_identity(st: os.stat_result) -> tuple:
    """What must not change while a snapshot is admitted. Excludes atime: hashing the file
    reads it, which can bump atime (relatime) and would fail a full stat_result comparison."""
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def open_snapshot(target: Path):
    target = target.resolve(strict=True)
    manifest_path = target.with_suffix('.manifest.json')
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw)
    stat = target.stat()
    signature = file_identity(stat)
    if (manifest.get('db') != str(target)
            or manifest.get('status') != 'frozen_current_vault_candidate'
            or manifest.get('schema') != 'validate_evidence_db'
            or manifest.get('integrity') != 'ok'
            or manifest.get('db_bytes') != stat.st_size
            or manifest.get('db_sha256') != sha(target)
            or any(Path(str(target) + s).exists() for s in ('-wal', '-journal'))):
        raise ValueError('snapshot_manifest_invalid')
    conn = connect_read_only(target, check_same_thread=False)
    try:
        validate_evidence_db(conn)
        if (conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok'
                or conn.execute('PRAGMA query_only').fetchone()[0] != 1
                or file_identity(target.stat()) != signature or sha(target) != manifest['db_sha256']
                or manifest_path.read_bytes() != raw):
            raise ValueError('snapshot_validation_failed')
        return conn, manifest['db_sha256']
    except BaseException:
        conn.close()
        raise


def replace_pointer(pointer: Path, target: Path):
    """Caller must hold the refresh lock; rename and fsync the containing directory."""
    temporary = pointer.with_name('.' + pointer.name + '.' + uuid.uuid4().hex)
    try:
        temporary.symlink_to(target.resolve(strict=True))
        os.replace(temporary, pointer)
        fd = os.open(pointer.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def _observe(path: Path) -> tuple[tuple, Path | None, str | None]:
    """(key, target, reason): `key` names what the pointer resolves to right now (target path,
    its file identity and its manifest's), so a rewrite in place is a new key too."""
    if not path.is_symlink():
        return ('pointer_not_symlink',), None, 'pointer_not_symlink'
    try:
        target = path.resolve(strict=True)
        identity = file_identity(target.stat())
    except (OSError, RuntimeError):
        try:
            link = os.readlink(path)
        except OSError:
            link = None
        return ('target_missing', link), None, 'target_missing'
    try:
        manifest = file_identity(target.with_suffix('.manifest.json').stat())
    except OSError:
        manifest = None
    return (str(target), identity, manifest), target, None


def _reason(exc: BaseException) -> str:
    """A reason class for a failed validation; never the exception's text."""
    token = exc.args[0] if isinstance(exc, ValueError) and exc.args else None
    if (token == 'snapshot_manifest_invalid' or isinstance(exc, FileNotFoundError)
            or isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError, AttributeError))):
        return 'manifest_invalid'  # missing, unreadable or not a JSON object
    if isinstance(exc, OSError):
        return 'io_error'
    return 'validation_failed'  # digest, integrity, schema or a file changed while hashing


def _digest(key: tuple) -> str:
    return hashlib.sha256(repr(key[0] if key else '').encode()).hexdigest()[:16]


def _built_at(target: Path) -> str | None:
    """The manifest's build time (`snapshot_contract.manifest_built_at`), best-effort: a plain
    DB without a manifest, or a manifest without the field, has none."""
    from .snapshot_contract import manifest_built_at
    try:
        manifest = json.loads(target.with_suffix('.manifest.json').read_bytes())
    except (OSError, ValueError):
        return None
    return manifest_built_at(manifest) if isinstance(manifest, dict) else None


def _stamp() -> str:
    return datetime.now(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _discard(conn) -> None:
    """Drop every per-connection cache, then close."""
    from .dense import _MATRIX_CACHE
    from .dated import drop_cache
    from .serving_cache import drop_cache as drop_serving_cache
    for key in list(_MATRIX_CACHE):
        if key[0] == id(conn):
            _MATRIX_CACHE.pop(key, None)
    drop_cache(conn)
    drop_serving_cache(conn)
    conn.close()


class ServingSnapshot:
    """The admitted snapshot and its lease lock; a pointer's admission runs in `poll()`."""

    def __init__(self, path, *, require: bool = True, clock=time.monotonic):
        self.path = Path(path).absolute()
        self.pointer = self.path.is_symlink()
        self.lock = threading.RLock()  # leases: no connection is closed beneath a call
        self._admit_lock = threading.Lock()  # one admission at a time
        self._clock = clock
        # Warm a validated candidate before it is swapped in: callable(conn), may raise.
        self.prepare = None
        self.conn = None
        self.snapshot_id = None
        self.built_at = None  # manifest `built_at` of the admitted snapshot (UTC ISO-8601)
        self.target = None
        self._key = None
        self._candidate = None
        self._rejected = None
        self._last_poll = None
        self._interval = None
        self._watcher = None
        self._stop = threading.Event()
        if not self.pointer:
            target = self.path.resolve(strict=True)
            conn = connect_read_only(target, check_same_thread=False)
            try:
                validate_evidence_db(conn)
                self.snapshot_id = sha(target)
            except BaseException:
                conn.close()
                raise
            self.conn, self.target, self.built_at = conn, target, _built_at(target)
            return
        if require:
            key, target, reason = _observe(self.path)
            if reason is not None:
                raise ValueError(reason)
            self.conn, self.snapshot_id = open_snapshot(target)
            self.target, self._key, self.built_at = target, key, _built_at(target)
        else:
            self.poll()

    # -------------------------------------------------------------- leases

    @contextmanager
    def lease(self):
        # Hold across retrieval: no connection can be closed beneath an in-flight call.
        with self.lock:
            if self.conn is None:
                raise SnapshotUnavailable('snapshot_unavailable')
            yield self.conn, self.snapshot_id

    # -------------------------------------------------------------- admission

    def poll(self, *, force: bool = False) -> str:
        """One admission step (the watcher's): returns the resulting state.

        `force` revalidates a rejected target before its retry delay.
        """
        if not self.pointer:
            return 'admitted'
        with self._admit_lock:
            self._last_poll = self._clock()
            key, target, reason = _observe(self.path)
            if key == self._key:
                self._candidate = self._rejected = None
                return 'admitted'
            rejected = self._rejected
            if (not force and rejected is not None and rejected['key'] == key
                    and self._clock() < rejected['retry_at']):
                return 'rejected'
            if self._candidate is None:
                self._candidate = {'since': self._clock(), 'since_at': _stamp()}
            if reason is None:
                reason = self._validate(key, target)
                if reason is None:
                    return 'admitted'
                if reason == 'pointer_changed':
                    return 'candidate'  # validated something the pointer no longer names
            self._reject(key, reason)
            return 'rejected'

    def _validate(self, key: tuple, target: Path) -> str | None:
        try:
            conn, identity = open_snapshot(target)
        except Exception as exc:
            return _reason(exc)
        try:
            if self.prepare is not None:
                self.prepare(conn)
        except Exception:
            _discard(conn)
            return 'warmup_failed'
        if _observe(self.path)[0] != key:
            _discard(conn)
            return 'pointer_changed'
        built_at = _built_at(target)
        with self.lock:
            old = self.conn
            self.conn, self.snapshot_id, self.target, self._key = conn, identity, target, key
            self.built_at = built_at
            self._candidate = self._rejected = None
            if old is not None:
                _discard(old)
        log.info('snapshot admitted %s', identity[:12])
        return None

    def _reject(self, key: tuple, reason: str) -> None:
        now = self._clock()
        previous = self._rejected
        same = previous is not None and previous['key'] == key
        attempts = previous['attempts'] + 1 if same else 1
        delay = REJECT_RETRY[min(attempts, len(REJECT_RETRY)) - 1]
        self._rejected = {'key': key, 'digest': _digest(key), 'reason': reason, 'attempts': attempts,
                          'since': previous['since'] if same else now,
                          'since_at': previous['since_at'] if same else _stamp(),
                          'retry_at': now + delay}
        self._candidate = None
        # No exception text/paths: manifests and payloads are untrusted.
        log.warning('snapshot pointer rejected (%s, target %s, attempt %d); %s', reason,
                    self._rejected['digest'], attempts,
                    'retaining admitted snapshot' if self.conn is not None else 'no snapshot admitted')

    # -------------------------------------------------------------- watcher

    def start_watcher(self, interval: float = WATCH_INTERVAL) -> "ServingSnapshot":
        """Poll the pointer every `interval` seconds in a daemon thread (plain DBs: no-op)."""
        if not self.pointer or self._watcher is not None:
            return self
        self._interval = interval
        self._last_poll = self._clock()

        def run():
            while not self._stop.wait(interval):
                try:
                    self.poll()
                except Exception:
                    log.warning('snapshot watcher poll failed')

        self._watcher = threading.Thread(target=run, name='quoth-snapshot-watch', daemon=True)
        self._watcher.start()
        return self

    def status(self) -> dict:
        """Admission state: what is served, what is pending, what was refused (no paths)."""
        now = self._clock()
        candidate, rejected = self._candidate, self._rejected
        state = ('unadmitted' if self.conn is None else 'candidate' if candidate is not None
                 else 'rejected' if rejected is not None else 'admitted')
        out = {'state': state, 'snapshot_id': self.snapshot_id, 'pointer': self.pointer}
        if candidate is not None:
            out['candidate'] = {'since': candidate['since_at'], 'age_s': round(now - candidate['since'], 1)}
        if rejected is not None:
            out['rejected'] = {'target_digest': rejected['digest'], 'reason': rejected['reason'],
                               'since': rejected['since_at'], 'attempts': rejected['attempts'],
                               'retry_in_s': max(0.0, round(rejected['retry_at'] - now, 1))}
        if self._watcher is not None:
            out['watcher'] = {'interval_s': self._interval,
                              'last_poll_age_s': round(now - self._last_poll, 1)}
        return out

    def health(self) -> dict:
        """`status()` plus a health token: ok, or why the served snapshot is not the pointer's."""
        out = self.status()
        candidate, watcher = out.get('candidate'), out.get('watcher')
        if self.conn is None:
            token = 'unavailable'
        elif 'rejected' in out:
            token = 'pointer_rejected'
        elif candidate is not None and candidate['age_s'] > CANDIDATE_MAX_AGE:
            token = 'candidate_stalled'
        elif (watcher is not None and candidate is None
              and watcher['last_poll_age_s'] > max(60.0, WATCHER_STALE_INTERVALS * watcher['interval_s'])):
            token = 'watcher_stalled'
        else:
            token = 'ok'
        return {'status': token, **out}

    def close(self):
        self._stop.set()
        with self._admit_lock, self.lock:
            if self.conn is not None:
                _discard(self.conn)
                self.conn = None
