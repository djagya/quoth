"""SQLite connections and the evidence schema (numbered, idempotent migrations)."""

from __future__ import annotations

import re
import sqlite3
from importlib.resources import files
from pathlib import Path

_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def list_migrations() -> list[tuple[int, str, str]]:
    pkg = files(f"{__package__}.migrations")
    out = []
    for entry in pkg.iterdir():
        m = _MIGRATION_RE.match(entry.name)
        if m:
            out.append((int(m.group(1)), entry.name, entry.read_text("utf-8")))
    out.sort()
    versions = [v for v, _, _ in out]
    if versions != list(range(1, len(versions) + 1)):
        raise RuntimeError(f"migration numbering must be contiguous from 0001: {versions}")
    return out


def connect(db_path: str | Path, check_same_thread: bool = True) -> sqlite3.Connection:
    p = Path(db_path)
    if str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), isolation_level=None, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def connect_read_only(db_path: str | Path, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open an existing DB without creating directories, journals or schema."""
    path = Path(db_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"read-only memory DB does not exist: {path}")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, isolation_level=None,
                           check_same_thread=check_same_thread)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        if conn.execute("PRAGMA query_only").fetchone()[0] != 1:
            raise RuntimeError("read-only memory DB: query_only could not be enabled")
        return conn
    except BaseException:
        conn.close()
        raise


def validate_evidence_db(conn: sqlite3.Connection) -> None:
    """Require the complete local schema and at least one current dense vector."""
    migrations = list_migrations()
    try:
        applied = conn.execute("SELECT version, name FROM schema_migrations ORDER BY version").fetchall()
        if [(v, n) for v, n, _ in migrations] != [tuple(row) for row in applied]:
            raise RuntimeError("read-only memory DB has missing/outdated schema migrations")
        required = {
            "sources": {"id", "path", "scope"},
            "source_versions": {"id", "source_id", "is_current"},
            "chunks": {"id", "source_version_id", "heading_path", "line_start", "line_end", "text"},
            "embeddings": {"chunk_id", "model", "dim", "vector"},
        }
        for table, columns in required.items():
            actual = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if not columns <= actual:
                raise RuntimeError(f"read-only memory DB missing required schema: {table}")
        if not conn.execute(
            "SELECT 1 FROM embeddings e JOIN chunks c ON c.id=e.chunk_id "
            "JOIN source_versions v ON v.id=c.source_version_id AND v.is_current=1 LIMIT 1"
        ).fetchone():
            raise RuntimeError("read-only memory DB has no current dense vectors")
    except sqlite3.DatabaseError as exc:
        raise RuntimeError(f"read-only memory DB missing/invalid schema: {exc}") from exc


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply pending migrations; returns names applied in this call."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL,"
        " applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))"
    )
    done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    applied = []
    for version, name, sql in list_migrations():
        if version in done:
            continue
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Re-check under the write lock (concurrent runners).
            if conn.execute("SELECT 1 FROM schema_migrations WHERE version=?", (version,)).fetchone():
                conn.execute("ROLLBACK")
                continue
            for stmt in _split_sql(sql):
                conn.execute(stmt)
            conn.execute("INSERT INTO schema_migrations(version, name) VALUES (?, ?)", (version, name))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        applied.append(name)
    return applied


def _split_sql(sql: str) -> list[str]:
    """Split a migration into complete statements (trigger bodies contain ';')."""
    stmts, buf = [], ""
    for line in sql.splitlines(keepends=True):
        if not buf and (not line.strip() or line.lstrip().startswith("--")):
            continue
        buf += line
        if sqlite3.complete_statement(buf):
            stmts.append(buf.strip())
            buf = ""
    if buf.strip():
        raise ValueError(f"incomplete SQL statement in migration: {buf[:80]!r}")
    return stmts


def open_db(db_path: str | Path) -> sqlite3.Connection:
    conn = connect(db_path)
    migrate(conn)
    return conn
