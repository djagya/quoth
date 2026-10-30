"""Serving's view of the index contract: links, indexed file facts, recall tiers and the
manifest build time.

Every serving read of those tables, columns and functions goes through this module, so
the serving side has one place to change when the index contract changes. Snapshots of an
older index contract never reach serving (their code digests are refused), so the reads
assume the current schema.

Contract:

- ``links(chunk_id, target, target_path, heading, line, kind, status)``: one row per link
  in a chunk; ``status`` is ``resolved`` (``target_path`` set), ``missing``, ``ambiguous``
  or ``excluded`` (the target was purged from the snapshot: never expose a path for it);
- the current ``source_versions`` row of a note holds its indexed content sha256
  (``content_hash``), and its mtime and size at indexing (``mtime_ns``, ``size_bytes``);
- ``quoth.scopes.explicit_scopes()``;
- the snapshot manifest's ``built_at`` (UTC ISO-8601).
"""
from __future__ import annotations

from dataclasses import dataclass

from . import scopes


# ---------------------------------------------------------------- links

def chunk_links(conn, chunk_ids: list[int], limit: int) -> dict[int, list[dict]]:
    """Per chunk, up to `limit` links in line order: ``{"path", "heading"}`` for a resolved
    one, ``{"text", "status"}`` otherwise (``excluded`` or ``unresolved``; never a path)."""
    if not chunk_ids:
        return {}
    rows = conn.execute(
        "SELECT chunk_id, target, target_path, heading, status FROM links"
        f" WHERE chunk_id IN ({','.join('?' * len(chunk_ids))}) ORDER BY chunk_id, line, rowid",
        list(chunk_ids)).fetchall()
    out: dict[int, list[dict]] = {}
    for cid, target, path, head, status in rows:
        items = out.setdefault(cid, [])
        if len(items) >= limit:
            continue
        if status == "resolved":
            item = {"path": path, "heading": head or None}
        elif status == "excluded":
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
    mtime_ns: int
    size: int


def indexed_files(conn, paths: list[str]) -> dict[str, IndexedFile]:
    """The indexed facts of each note's current version."""
    if not paths:
        return {}
    rows = conn.execute(
        "SELECT s.path, v.content_hash, v.mtime_ns, v.size_bytes FROM sources s"
        " JOIN source_versions v ON v.source_id = s.id AND v.is_current = 1"
        f" WHERE s.path IN ({','.join('?' * len(paths))})", list(paths)).fetchall()
    return {r[0]: IndexedFile(r[1], r[2], r[3]) for r in rows}


# ---------------------------------------------------------------- recall tiers

def explicit_scopes() -> frozenset[str]:
    """Scopes served only on explicit request."""
    return scopes.explicit_scopes()


# ---------------------------------------------------------------- manifest

def manifest_built_at(manifest: dict | None) -> str | None:
    value = (manifest or {}).get("built_at")
    return value if isinstance(value, str) and value else None
