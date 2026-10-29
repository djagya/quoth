"""Serving's view of the index contract added in 0.17: links, indexed file facts, recall
tiers and the manifest build time.

Every read of those tables, columns and functions goes through this module, so the
serving side has one place to change when the index side settles a name. Each read is
tolerant: a snapshot without the table or column (an older build, a test fixture) gives
empty results, never an error, and the feature that needs it is simply absent.

Contract:

- ``links(chunk_id, target, target_path, heading, line, status)``: one row per link in a
  chunk; ``status`` is ``resolved`` (``target_path`` set), ``missing``, ``ambiguous`` or
  ``excluded`` (the target was purged from the snapshot: never expose a path for it);
- the current ``source_versions`` row of a note holds its indexed content sha256
  (``content_hash``), and its mtime and size at indexing (`MTIME_COLUMN`, `SIZE_COLUMN`);
- ``quoth.scopes.recall_tier(scope)`` and ``quoth.scopes.explicit_scopes()``;
- the snapshot manifest's ``built_at`` (UTC ISO-8601).
"""
from __future__ import annotations

from dataclasses import dataclass
import sqlite3

LINKS_TABLE = "links"
MTIME_COLUMN = "mtime_ns"
SIZE_COLUMN = "size_bytes"
BUILT_AT_KEY = "built_at"

_COLUMNS: dict = {}  # (id(conn), table) -> frozenset of column names


def _columns(conn, table: str) -> frozenset[str]:
    key = (id(conn), table)
    hit = _COLUMNS.get(key)
    if hit is None:
        try:
            hit = frozenset(r[1] for r in conn.execute(f"PRAGMA table_info({table})"))
        except sqlite3.DatabaseError:
            hit = frozenset()
        if len(_COLUMNS) > 64:
            _COLUMNS.clear()
        _COLUMNS[key] = hit
    return hit


def drop_cache(conn) -> None:
    for key in [k for k in _COLUMNS if k[0] == id(conn)]:
        _COLUMNS.pop(key, None)


# ---------------------------------------------------------------- links

def chunk_links(conn, chunk_ids: list[int], limit: int) -> dict[int, list[dict]]:
    """Per chunk, up to `limit` links in line order: ``{"path", "heading"}`` for a resolved
    one, ``{"text", "status"}`` otherwise (``excluded`` or ``unresolved``; never a path)."""
    cols = _columns(conn, LINKS_TABLE)
    if not chunk_ids or not {"chunk_id", "target", "target_path"} <= cols:
        return {}
    status = "status" if "status" in cols else "CASE WHEN target_path IS NULL THEN 'missing' ELSE 'resolved' END"
    heading = "heading" if "heading" in cols else "NULL"
    line = "line" if "line" in cols else "0"
    rows = conn.execute(
        f"SELECT chunk_id, target, target_path, {heading}, {status} FROM {LINKS_TABLE}"
        f" WHERE chunk_id IN ({','.join('?' * len(chunk_ids))}) ORDER BY chunk_id, {line}, rowid",
        list(chunk_ids)).fetchall()
    out: dict[int, list[dict]] = {}
    for cid, target, path, head, st in rows:
        items = out.setdefault(cid, [])
        if len(items) >= limit:
            continue
        if st == "resolved" and path:
            item = {"path": path, "heading": head or None}
        elif st == "excluded":
            item = {"text": str(target or ""), "status": "excluded"}
        else:
            item = {"text": str(target or ""), "status": "unresolved"}
        if item not in items:
            items.append(item)
    return out


# ---------------------------------------------------------------- indexed file facts

@dataclass(frozen=True)
class IndexedFile:
    sha256: str
    mtime_ns: int | None
    size: int | None


def indexed_files(conn, paths: list[str]) -> dict[str, IndexedFile]:
    """The indexed facts of each note's current version (mtime/size None when not recorded)."""
    if not paths:
        return {}
    cols = _columns(conn, "source_versions")
    mtime = f"v.{MTIME_COLUMN}" if MTIME_COLUMN in cols else "NULL"
    size = f"v.{SIZE_COLUMN}" if SIZE_COLUMN in cols else "NULL"
    rows = conn.execute(
        f"SELECT s.path, v.content_hash, {mtime}, {size} FROM sources s"
        " JOIN source_versions v ON v.source_id = s.id AND v.is_current = 1"
        f" WHERE s.path IN ({','.join('?' * len(paths))})", list(paths)).fetchall()
    return {r[0]: IndexedFile(r[1], r[2], r[3]) for r in rows}


# ---------------------------------------------------------------- recall tiers

def explicit_scopes() -> frozenset[str]:
    """Scopes served only on explicit request (`quoth.scopes.explicit_scopes`); none when
    the index side does not define tiers."""
    from . import scopes
    fn = getattr(scopes, "explicit_scopes", None)
    return frozenset(fn()) if fn is not None else frozenset()


# ---------------------------------------------------------------- manifest

def manifest_built_at(manifest: dict | None) -> str | None:
    value = (manifest or {}).get(BUILT_AT_KEY)
    return value if isinstance(value, str) and value else None
