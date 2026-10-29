"""Atomic, fail-closed snapshot pointer admission and serialized connection leases."""
from __future__ import annotations

from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import threading
import uuid

from .db import connect_read_only, validate_evidence_db
from .refresh import sha


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


class ServingSnapshot:
    def __init__(self, path):
        self.path = Path(path).absolute()
        self.pointer = self.path.is_symlink()
        self.lock = threading.RLock()
        self.target = self.path.resolve(strict=True)
        if self.pointer:
            self.conn, self.snapshot_id = open_snapshot(self.target)
        else:
            self.conn = connect_read_only(self.target, check_same_thread=False)
            try:
                validate_evidence_db(self.conn)
                self.snapshot_id = sha(self.target)
            except BaseException:
                self.conn.close()
                raise

    @contextmanager
    def lease(self):
        # Hold across retrieval: no connection can be closed beneath an in-flight call.
        with self.lock:
            if self.pointer:
                try:
                    if not self.path.is_symlink():
                        raise ValueError('pointer_not_symlink')
                    target = self.path.resolve(strict=True)
                    if target != self.target:
                        conn, identity = open_snapshot(target)
                        if self.path.resolve(strict=True) != target:
                            conn.close()
                            raise ValueError('pointer_changed_during_validation')
                        old = self.conn
                        self.conn, self.target, self.snapshot_id = conn, target, identity
                        from .dense import _MATRIX_CACHE
                        from .dated import drop_cache
                        from .serving_cache import drop_cache as drop_serving_cache
                        for key in list(_MATRIX_CACHE):
                            if key[0] == id(old):
                                del _MATRIX_CACHE[key]
                        drop_cache(old)
                        drop_serving_cache(old)
                        old.close()
                except Exception:
                    # No exception text/paths: manifests and payloads are untrusted.
                    logging.getLogger(__name__).warning('snapshot pointer rejected; retaining admitted snapshot')
            yield self.conn, self.snapshot_id

    def close(self):
        with self.lock:
            from .dated import drop_cache
            from .serving_cache import drop_cache as drop_serving_cache
            drop_cache(self.conn)
            drop_serving_cache(self.conn)
            self.conn.close()
