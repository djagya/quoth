"""Portable immutable full and scoped snapshot bundles.

A bundle is a pair of files: ``<name>.db`` — a self-contained SQLite snapshot
built fresh under the current schema — and ``<name>.manifest.json``. The
manifest is serialized into a private staging area, fsynced there, and only
then hard-linked into place without overwrite, so a manifest found at its
final path is always a complete document; its existence marks the bundle
complete.

Two invariants shape the design:

- Never copy an active writer's database file. The source is captured with
  the SQLite online-backup API into a private working snapshot, opened
  read-only (``query_only``); the bundle is then built from that snapshot by
  ``INSERT ... SELECT`` into a freshly migrated database.
- Scoped bundles physically omit excluded content. Excluded rows are never
  written into the bundle file (no copy-then-delete), derived tables (FTS)
  are rebuilt from the selected chunks only, and the finished file is
  vacuumed. Scope closure covers sources → versions → chunks → entities /
  embeddings / FTS.

A third invariant governs the export's own files:

- Operation-owned private staging. Everything an export touches before
  publication lives in one sibling ``<name>.stage`` directory created
  ``0700`` with ``0600`` files (umask-independent) and an ``owner.json``
  ownership record. No path outside staging is created, unlinked or
  replaced; foreign files, symlinks and live owners are refused, never
  repaired. Publication is always a no-overwrite hard link, so a crash can
  leave a bundle without its manifest but never a partial manifest, a
  clobbered foreign artifact, or world-readable full-corpus staging.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .db import connect_read_only, list_migrations, migrate
from .refresh import code_digests

BUNDLE_FORMAT = "quoth-bundle/1"
SUPPORTED_FORMATS = frozenset({BUNDLE_FORMAT})

#: Logical data tables in insert (dependency) order.
CONTENT_TABLES = ("sources", "source_versions", "chunks", "entities", "embeddings")
#: Rebuilt from the selected current chunks; never copied row-for-row.
DERIVED_TABLES = ("chunks_fts", "chunks_fts_trigram")

#: Identity of the private operation-owned staging layout.
STAGE_FORMAT = "quoth-bundle-stage/1"
_STAGE_DIR_SUFFIX = ".stage"
_STAGE_OWNED_EXACT = frozenset({
    "owner.json", "owner.json.tmp",
    "checkpoint.json", "checkpoint.json.tmp",
    "snapshot.db", "bundle.db", "manifest.json.tmp",
})
#: SQLite journal/wal/shm sidecars of the staged databases.
_STAGE_OWNED_PREFIXES = ("snapshot.db-", "bundle.db-")
#: Staging files rebuilt from scratch on every attempt; safe to remove once
#: the directory is verified as ours.
_STAGE_REBUILDABLE = ("snapshot.db", "bundle.db", "manifest.json.tmp",
                      "owner.json.tmp", "checkpoint.json.tmp")

# Row predicate of each content table in a scoped bundle (the scope closure).
_SCOPED_PREDICATES = {
    "sources": "scope = :scope",
    "source_versions": "source_id IN (SELECT id FROM main.sources)",
    "chunks": "source_version_id IN (SELECT id FROM main.source_versions)",
    "entities": "chunk_id IN (SELECT id FROM main.chunks)",
    "embeddings": "chunk_id IN (SELECT id FROM main.chunks)",
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%fZ")


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for buf in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(buf)
    return h.hexdigest()


def _fsync_dir(path: Path) -> None:
    """Persist a directory's entries (link/unlink durability)."""
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _create_private_file(path: Path) -> None:
    """Create an empty operation-owned file with mode 0600.

    0600 contains only owner bits, so no umask can make it less private.
    O_EXCL guarantees the path is fresh: a foreign file (or symlink, dangling
    included) surfaces as FileExistsError instead of being repaired.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)


def _serialize_private_json(path: Path, obj: dict) -> None:
    """Serialize JSON into a staging-owned file: write + fsync, nothing else.

    Created/truncated 0600 regardless of umask. Publication to a final path
    is a separate step, so a crash mid-serialization can never leave partial
    content at a published name.
    """
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())


def _write_staging_json(path: Path, obj: dict) -> None:
    """Atomically (re)write a JSON file inside our private staging directory.

    tmp + fsync + ``os.replace`` + directory fsync: a crash leaves either the
    old or the new complete file, never a partial one. Replace semantics are
    safe only for staging-internal names, which no foreign observer can see.
    """
    tmp = path.with_name(path.name + ".tmp")
    _serialize_private_json(tmp, obj)
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _publish_without_overwrite(src: Path, dst: Path, message: str) -> None:
    """Publish the fsynced ``src`` at ``dst`` by hard link; never overwrite.

    link(2) fails atomically with EEXIST if ``dst`` exists in any form
    (regular file, symlink, dangling symlink), so already-published outputs
    and foreign artifacts are preserved; the staged original stays in place
    for a bounded recovery decision.
    """
    try:
        os.link(src, dst)
    except FileExistsError:
        raise ValueError(message) from None
    _fsync_dir(dst.parent)


# --------------------------------------------------------------------------
# Canonical logical digests (survive VACUUM / page-level differences)
# --------------------------------------------------------------------------

def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _pk_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [c for _, c in sorted((r[5], r[1]) for r in info if r[5] > 0)]


def _canonical(value):
    if isinstance(value, bytes):
        return {"blob": base64.b64encode(value).decode("ascii")}
    return value


def table_digest(conn: sqlite3.Connection, table: str) -> str:
    """Digest of a table's logical rows, ordered by primary key."""
    cols = ", ".join(_columns(conn, table))
    order = ", ".join(_pk_columns(conn, table)) or "rowid"
    h = hashlib.sha256()
    for row in conn.execute(f"SELECT {cols} FROM {table} ORDER BY {order}"):
        h.update(json.dumps([_canonical(v) for v in row],
                            ensure_ascii=False, separators=(",", ":")).encode())
        h.update(b"\n")
    return h.hexdigest()


def corpus_digest(conn: sqlite3.Connection, tables: tuple[str, ...]) -> tuple[str, dict[str, str]]:
    per = {t: table_digest(conn, t) for t in tables}
    core = json.dumps(per, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(core).hexdigest(), per


def table_counts(conn: sqlite3.Connection, tables: tuple[str, ...]) -> dict[str, int]:
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}


def _schema_identity(conn: sqlite3.Connection) -> list[list[int | str]]:
    return [[v, n] for v, n in conn.execute(
        "SELECT version, name FROM schema_migrations ORDER BY version")]


def _current_schema_identity() -> list[list[int | str]]:
    return [[v, n] for v, n, _ in list_migrations()]


def _schema_digest(identity: list[list[int | str]]) -> str:
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


# --------------------------------------------------------------------------
# Source capture (consistent snapshot; never a raw file copy)
# --------------------------------------------------------------------------

def _check_source_schema(conn: sqlite3.Connection) -> None:
    if _schema_identity(conn) != _current_schema_identity():
        raise ValueError("source schema not recognized (schema_migrations differs from "
                         "this code); incompatible source version")


def capture_snapshot(source: Path, work: Path) -> dict:
    """Point-in-time consistent copy of the source via the SQLite backup API.

    The source is opened read-only (``query_only``) and never written; the
    backup runs inside a read transaction, so a concurrently active writer
    cannot tear the captured image. ``work`` must be a fresh private path:
    it is created O_EXCL with mode 0600, and an existing file — foreign or
    ours — is never clobbered here. The working copy is collapsed to a
    single self-contained file before it is hashed.
    """
    _create_private_file(work)
    with closing(connect_read_only(source)) as src:
        _check_source_schema(src)
        with closing(sqlite3.connect(work, isolation_level=None)) as dst:
            src.backup(dst)
            dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            dst.execute("PRAGMA journal_mode=DELETE")
    return {"capture": "sqlite_backup", "snapshot_sha256": sha(work),
            "snapshot_bytes": work.stat().st_size}


# --------------------------------------------------------------------------
# Operation-owned staging: collision safety and bounded recovery
# --------------------------------------------------------------------------

def _stage_paths(target: Path) -> dict[str, Path]:
    stage = target.with_name(target.name + _STAGE_DIR_SUFFIX)
    return {"stage": stage, "owner": stage / "owner.json",
            "checkpoint": stage / "checkpoint.json",
            "snapshot": stage / "snapshot.db",
            "bundle": stage / "bundle.db",
            "manifest_tmp": stage / "manifest.json.tmp"}


def _owner_record(target: Path, source: Path, scope: str | None) -> dict:
    return {"stage_format": STAGE_FORMAT, "target": str(target),
            "source": str(source), "scope": scope,
            "pid": os.getpid(), "thread": threading.get_ident(),
            "created_at": _now()}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # exists but is not signalable by us: treat as live
    return True


def _check_stage_entries(stage: Path) -> None:
    """Every staging entry must be a regular file with an owned name.

    Foreign names and symlinks are refused, never unlinked: a symlink could
    redirect writes outside staging, and an unknown name means the directory
    is not verifiably ours.
    """
    for entry in os.scandir(stage):
        if entry.is_symlink():
            raise ValueError(
                f"staging {stage} contains symlink entry {entry.name!r}; refusing")
        owned = (entry.name in _STAGE_OWNED_EXACT
                 or entry.name.startswith(_STAGE_OWNED_PREFIXES))
        if not owned or not entry.is_file(follow_symlinks=False):
            raise ValueError(
                f"staging {stage} contains foreign entry {entry.name!r}; refusing "
                "(remove it manually if you placed it there)")


def _read_staging_json(path: Path) -> dict:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise ValueError(f"{path.name} is not a JSON object")
    return obj


def _stage_fresh(paths: dict[str, Path], target: Path, source: Path,
                 scope: str | None) -> None:
    """Create the private staging directory; refuse if anything exists there."""
    stage = paths["stage"]
    try:
        os.mkdir(stage, 0o700)  # umask only clears bits, so 0700 survives any umask
    except FileExistsError:
        raise ValueError(
            f"output target collision; staging {stage} already exists; use "
            "resume=True for an incomplete checkpointed export") from None
    _fsync_dir(stage.parent)
    _write_staging_json(paths["owner"], _owner_record(target, source, scope))


def _stage_takeover(paths: dict[str, Path], target: Path, source: Path,
                    scope: str | None) -> dict | None:
    """Verify an interrupted export's staging and prepare it for reuse.

    Ownership is explicit: the owner record must name this exact export, and
    a live owner (another process, or another thread of this process) is
    never displaced. Returns the saved checkpoint dict, or None if none was
    published yet. Refuses — without deleting anything — on foreign,
    mismatched or malformed state; only verifiably owned rebuildable files
    are removed.
    """
    stage = paths["stage"]
    if stage.is_symlink() or not stage.is_dir():
        raise ValueError("no resumable checkpoint for this target")
    try:
        owner = _read_staging_json(paths["owner"])
    except FileNotFoundError:
        # Crash before the owner record landed: adopt the directory only if
        # nothing in it is foreign, then continue as a fresh export in place.
        _check_stage_entries(stage)
        _write_staging_json(paths["owner"], _owner_record(target, source, scope))
        return None
    except json.JSONDecodeError:
        raise ValueError(
            f"staging owner record {paths['owner']} is malformed; refusing to "
            "resume (not created by a compatible export)") from None
    expected = {"stage_format": STAGE_FORMAT, "target": str(target),
                "source": str(source), "scope": scope}
    if any(owner.get(k) != v for k, v in expected.items()):
        raise ValueError(
            "staging owner record does not match this export (target, source "
            "or scope differ); refusing to resume another operation's staging")
    pid, thread = owner.get("pid"), owner.get("thread")
    if not isinstance(pid, int) or not isinstance(thread, int):
        raise ValueError("staging owner record is malformed; refusing to resume")
    if not (pid == os.getpid() and thread == threading.get_ident()) \
            and _pid_alive(pid):
        raise ValueError(
            f"staging is owned by a live operation (pid {pid}); refusing to "
            "resume concurrently — let it finish or resume after it exits")
    _check_stage_entries(stage)
    checkpoint = None
    if os.path.lexists(paths["checkpoint"]):
        try:
            checkpoint = _read_staging_json(paths["checkpoint"])
        except json.JSONDecodeError:
            # Checkpoint writes are atomic (tmp+replace): a present but
            # unparseable checkpoint was not produced by this code.
            raise ValueError(
                "staging checkpoint is malformed; refusing to resume "
                "(not produced by a compatible export)") from None
    for name in _STAGE_REBUILDABLE:
        p = stage / name
        if os.path.lexists(p):
            os.unlink(p)
    for entry in os.scandir(stage):  # stale sqlite journal/wal/shm sidecars
        if entry.name.startswith(_STAGE_OWNED_PREFIXES):
            os.unlink(entry.path)
    _fsync_dir(stage)
    return checkpoint


# --------------------------------------------------------------------------
# Bundle construction
# --------------------------------------------------------------------------

def _rebuild_fts(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT c.id, s.path, c.heading_path, c.text FROM chunks c"
        " JOIN source_versions v ON v.id = c.source_version_id AND v.is_current = 1"
        " JOIN sources s ON s.id = v.source_id").fetchall()
    for tbl in DERIVED_TABLES:
        conn.execute(f"DELETE FROM {tbl}")
        conn.executemany(
            f"INSERT INTO {tbl}(rowid, title, heading_path, text) VALUES (?, ?, ?, ?)",
            ((r[0], PurePosixPath(r[1]).stem, r[2], r[3]) for r in rows))


def _build_bundle(target_work: Path, snapshot: Path, scope: str | None) -> dict:
    """Build the bundle database from the captured snapshot into a fresh file."""
    _create_private_file(target_work)
    conn = sqlite3.connect(target_work, isolation_level=None)
    conn.row_factory = sqlite3.Row
    selection: dict[str, dict] = {}
    try:
        migrate(conn)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"ATTACH DATABASE '{snapshot.as_posix()}' AS srcsnap")
        try:
            for table in CONTENT_TABLES:
                cols = ", ".join(_columns(conn, table))
                if scope is None:
                    conn.execute(f"INSERT INTO main.{table} ({cols})"
                                 f" SELECT {cols} FROM srcsnap.{table}")
                    selection[table] = {"mode": "full"}
                else:
                    cur = conn.execute(
                        f"INSERT INTO main.{table} ({cols}) SELECT {cols} FROM srcsnap.{table}"
                        f" WHERE {_SCOPED_PREDICATES[table]}", {"scope": scope})
                    eligible = conn.execute(
                        f"SELECT count(*) FROM srcsnap.{table}").fetchone()[0]
                    selection[table] = {"mode": "scoped", "eligible": eligible,
                                        "kept": cur.rowcount}
        finally:
            conn.execute("DETACH DATABASE srcsnap")
        _rebuild_fts(conn)
        # Physical compaction: excluded content was never written; VACUUM also
        # guarantees no residual free-list pages from the build itself.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("VACUUM")
        counts = table_counts(conn, CONTENT_TABLES)
        digest, per_table = corpus_digest(conn, CONTENT_TABLES)
        models = [{"model": r[0], "dim": r[1], "vectors": r[2]} for r in conn.execute(
            "SELECT model, dim, count(*) FROM embeddings GROUP BY model, dim ORDER BY model")]
        _verify_structure(conn, scope=scope)
        return {"selection": selection, "counts": counts, "models": models,
                "corpus_digest": digest, "corpus_per_table": per_table}
    finally:
        conn.close()


def export_bundle(source: str | Path, target: str | Path, *, scope: str | None = None,
                  resume: bool = False) -> dict:
    """Freeze a portable bundle (full, or scoped to one source-scope label).

    Never writes the source. All pre-publication state lives in one private
    ``<target>.stage`` directory (0700/0600, ownership-recorded); nothing
    outside it is created, removed or replaced. The bundle db and the
    manifest are published by hard link without overwrite — an existing
    file at either name is preserved and reported — so the manifest's
    existence marks the bundle complete and is always a complete document.
    ``resume=True`` recovers an interrupted export of the exact same
    source/target/parameters, adopting a partially published db only when
    it is the one this staging recorded.
    """
    source, target = Path(source).resolve(), Path(target).absolute()
    if scope is not None and not scope.strip():
        raise ValueError("scope must be a non-empty label or None")
    scope = scope.strip() if scope else None
    if source == target or target.is_symlink() or not source.is_file():
        raise ValueError("source/target invalid")
    manifest_path = target.with_suffix(".manifest.json")
    if os.path.lexists(manifest_path):
        raise ValueError("bundle manifest already exists (frozen output)")
    target.parent.mkdir(parents=True, exist_ok=True)
    paths = _stage_paths(target)
    if resume:
        saved_checkpoint = _stage_takeover(paths, target, source, scope)
    else:
        if os.path.lexists(target):
            raise ValueError("output target collision; target exists and no "
                             "resume was requested")
        _stage_fresh(paths, target, source, scope)
        saved_checkpoint = None
    capture = capture_snapshot(source, paths["snapshot"])
    core = {"format": BUNDLE_FORMAT, "kind": "scoped" if scope else "full", "scope": scope,
            "source_db": str(source), "capture": capture["capture"],
            "snapshot_sha256": capture["snapshot_sha256"],
            "snapshot_bytes": capture["snapshot_bytes"],
            "bundle_py_sha256": sha(Path(__file__).resolve())}
    if saved_checkpoint is None:
        checkpoint = {**core, "started_at": _now()}
        _write_staging_json(paths["checkpoint"], checkpoint)
    else:
        if {k: saved_checkpoint.get(k) for k in core} != core:
            raise ValueError("checkpoint does not match this export (source drifted "
                             "or parameters changed); start a new unique target")
        checkpoint = saved_checkpoint
    with closing(sqlite3.connect(paths["snapshot"], isolation_level=None)) as snap:
        snapshot_counts = table_counts(snap, CONTENT_TABLES)
    built = _build_bundle(paths["bundle"], paths["snapshot"], scope)
    if scope is None:
        if built["counts"] != snapshot_counts:
            raise ValueError("full bundle inventory differs from source snapshot")
    elif built["counts"]["sources"] == 0:
        raise ValueError(f"scope {scope!r} matches no sources; refusing an empty "
                         "scoped bundle (misconfigured scope label?)")
    # Bounded recovery of the crash window between the two publications:
    # adopt the target only when it is the exact artifact this staging
    # recorded; any other existing file is foreign and never overwritten.
    adopted = False
    if os.path.lexists(target):
        published = checkpoint.get("published_db_sha256")
        if (isinstance(published, str) and target.is_file()
                and not target.is_symlink() and sha(target) == published):
            db_digest, db_bytes = published, target.stat().st_size
            adopted = True
        else:
            raise ValueError(
                "output target exists but is not the artifact this staging "
                "published; refusing to overwrite a file this export does not own")
    else:
        db_digest, db_bytes = sha(paths["bundle"]), paths["bundle"].stat().st_size
    tables_meta = {table: {"count": built["counts"][table], "state": "included"}
                   for table in CONTENT_TABLES}
    schema_identity = _current_schema_identity()
    manifest = {
        "format": BUNDLE_FORMAT,
        "bundle_id": hashlib.sha256(json.dumps({
            "format": BUNDLE_FORMAT, "kind": core["kind"], "scope": scope,
            "db_sha256": db_digest, "corpus_sha256": built["corpus_digest"],
            "schema_sha256": _schema_digest(schema_identity)},
            sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "kind": core["kind"], "scope": scope,
        "status": "complete", "errors": [],
        "created_at": _now(),
        "source": {"db": str(source), **capture, "table_counts": snapshot_counts},
        "selection": built["selection"],
        "tables": tables_meta,
        "derived": {t: {"state": "rebuilt-from-current-chunks"} for t in DERIVED_TABLES},
        "schema": {"identity": "schema_migrations", "migrations": schema_identity,
                   "migrations_sha256": _schema_digest(schema_identity)},
        "engine": {"sqlite_version": sqlite3.sqlite_version},
        "models": built["models"],
        "pipeline": {"contract": "bundle.py", "bundle_py_sha256": core["bundle_py_sha256"],
                     "code_digests": code_digests()},
        "corpus": {"digest": built["corpus_digest"], "per_table": built["corpus_per_table"]},
        "db": {"bytes": db_bytes, "sha256": db_digest},
        "compatibility": {"format": BUNDLE_FORMAT,
                          "schema": "exact schema_migrations match required"},
    }
    # Serialize + fsync inside staging first: a crash here leaves nothing at
    # any published name. Record publish intent before the first link so an
    # interrupted publication is recognizable and adoptable on resume.
    _serialize_private_json(paths["manifest_tmp"], manifest)
    checkpoint = {**checkpoint, "published_db_sha256": db_digest}
    _write_staging_json(paths["checkpoint"], checkpoint)
    if not adopted:
        _publish_without_overwrite(
            paths["bundle"], target,
            "output target collision; target appeared during this export")
    os.chmod(target, 0o444)
    fd = os.open(target, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    _publish_without_overwrite(
        paths["manifest_tmp"], manifest_path,
        "bundle manifest already exists (frozen output)")
    os.chmod(manifest_path, 0o444)
    os.unlink(paths["manifest_tmp"])  # same inode as the published manifest
    shutil.rmtree(paths["stage"])
    _fsync_dir(target.parent)
    return manifest


# --------------------------------------------------------------------------
# Validation and import
# --------------------------------------------------------------------------

def load_manifest(manifest_path: str | Path) -> dict:
    m = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    fmt = m.get("format") if isinstance(m, dict) else None
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(f"unsupported bundle format: {fmt!r}")
    for key in ("kind", "scope", "tables", "corpus", "db", "schema", "models"):
        if key not in m:
            raise ValueError(f"bundle manifest missing required field: {key}")
    return m


def _verify_fts_parity(conn: sqlite3.Connection) -> None:
    current = {r[0] for r in conn.execute(
        "SELECT c.id FROM chunks c JOIN source_versions v"
        " ON v.id=c.source_version_id AND v.is_current=1")}
    for tbl in DERIVED_TABLES:
        fts = {r[0] for r in conn.execute(f"SELECT rowid FROM {tbl}")}
        if fts != current:
            raise ValueError(f"derived index {tbl} does not match current chunks")


def _verify_structure(conn: sqlite3.Connection, *, scope: str | None) -> None:
    """Invariants that hold for every well-formed bundle, manifest-independent."""
    if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ValueError("bundle SQLite integrity_check failed")
    if conn.execute("PRAGMA foreign_key_check").fetchone():
        raise ValueError("bundle foreign keys invalid (orphaned or leaked rows)")
    if _schema_identity(conn) != _current_schema_identity():
        raise ValueError("bundle schema incompatible with this code version")
    _verify_fts_parity(conn)
    if scope is not None:
        if conn.execute("SELECT 1 FROM sources WHERE scope != ? LIMIT 1", (scope,)).fetchone():
            raise ValueError("scoped bundle contains sources outside the declared scope")


def _verify_against_manifest(conn: sqlite3.Connection, m: dict) -> None:
    """Manifest-declared facts: schema, per-table counts, corpus digest, models."""
    if _schema_identity(conn) != [list(x) for x in m["schema"]["migrations"]]:
        raise ValueError("bundle schema_migrations differs from manifest "
                         "(tampered or misdeclared)")
    counts = table_counts(conn, CONTENT_TABLES)
    for table, meta in m["tables"].items():
        if meta["state"] == "included":
            if counts.get(table) != meta["count"]:
                raise ValueError(f"table {table} count {counts.get(table)} != "
                                 f"manifest {meta['count']}")
        elif counts.get(table):
            raise ValueError(f"table {table} declared {meta['state']!r} but not empty")
    digest, per_table = corpus_digest(conn, CONTENT_TABLES)
    if digest != m["corpus"]["digest"] or per_table != m["corpus"]["per_table"]:
        raise ValueError("bundle corpus digest mismatch (logical content differs "
                         "from manifest)")
    models = [{"model": r[0], "dim": r[1], "vectors": r[2]} for r in conn.execute(
        "SELECT model, dim, count(*) FROM embeddings GROUP BY model, dim ORDER BY model")]
    if models != m["models"]:
        raise ValueError("bundle embedding models differ from manifest")


def validate_bundle(db_path: str | Path, manifest_path: str | Path | None = None) -> dict:
    """Verify a bundle end-to-end: format, digests, schema, inventory, scope."""
    db_path = Path(db_path)
    if not db_path.is_file():
        raise FileNotFoundError(f"bundle db not found: {db_path}")
    if manifest_path is None:
        manifest_path = db_path.with_suffix(".manifest.json")
    m = load_manifest(manifest_path)
    digest, size = sha(db_path), db_path.stat().st_size
    if digest != m["db"]["sha256"] or size != m["db"]["bytes"]:
        raise ValueError("bundle db bytes/digest differ from manifest (tampered or truncated)")
    if m.get("status") != "complete" or m.get("errors"):
        raise ValueError("bundle manifest is not a clean completion record")
    with closing(connect_read_only(db_path)) as conn:
        _verify_structure(conn, scope=m.get("scope"))
        _verify_against_manifest(conn, m)
    return m


def import_bundle(db_path: str | Path, target: str | Path,
                  manifest_path: str | Path | None = None) -> dict:
    """Validate a bundle and restore it into a fresh usable local database."""
    db_path, target = Path(db_path).resolve(), Path(target).absolute()
    m = validate_bundle(db_path, manifest_path)
    if db_path == target:
        raise ValueError("import target must differ from the bundle")
    if os.path.lexists(target):
        raise ValueError("import target already exists; bundles restore into fresh targets")
    target.parent.mkdir(parents=True, exist_ok=True)
    # O_EXCL closes the check/create race (exactly one of two colliding
    # imports proceeds) and keeps the target private (0600) from creation.
    try:
        _create_private_file(target)
    except FileExistsError:
        raise ValueError("import target already exists; bundles restore into "
                         "fresh targets") from None
    try:
        with closing(connect_read_only(db_path)) as src, \
                closing(sqlite3.connect(target, isolation_level=None)) as dst:
            src.backup(dst)
            dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            dst.execute("PRAGMA journal_mode=DELETE")
        with closing(sqlite3.connect(target, isolation_level=None)) as conn:
            conn.row_factory = sqlite3.Row
            _verify_structure(conn, scope=m.get("scope"))
            _verify_against_manifest(conn, m)
            counts = table_counts(conn, CONTENT_TABLES)
            drift = []
            declared = m["pipeline"]["code_digests"]
            current = code_digests()
            changed = {k for k in set(current) | set(declared)
                       if current.get(k) != declared.get(k)}
            if changed:
                drift.append({"kind": "pipeline-code", "changed": sorted(changed)})
            if m["engine"]["sqlite_version"] != sqlite3.sqlite_version:
                drift.append({"kind": "engine", "manifest": m["engine"]["sqlite_version"],
                              "local": sqlite3.sqlite_version})
    except BaseException:
        # The target was created by this call: the partial artifact is owned
        # and removed. A pre-existing foreign target was never reached.
        try:
            os.unlink(target)
        except FileNotFoundError:
            pass
        raise
    return {"bundle_id": m["bundle_id"], "kind": m["kind"], "scope": m.get("scope"),
            "target": str(target), "table_counts": counts,
            "corpus_digest": m["corpus"]["digest"], "models": m["models"],
            "compatibility_drift": drift, "source": m["source"]}
