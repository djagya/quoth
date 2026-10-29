"""Incremental, idempotent vault ingest (read-only on the vault)."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .chunker import chunk_markdown
from .entities import entities_for_chunk
from .links import Resolver, extract_links
from .scopes import load_config
from .vault import IgnoreRules, frontmatter_excluded, path_excluded, walk


@dataclass
class IngestStats:
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    removed: int = 0
    excluded: int = 0
    purged: int = 0
    chunks_added: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "new": self.new, "changed": self.changed, "unchanged": self.unchanged,
            "removed": self.removed, "excluded": self.excluded, "purged": self.purged,
            "chunks_added": self.chunks_added, "errors": self.errors,
        }


def iter_vault_notes(vault: Path):
    """Yield (rel_posix_path, abs_path) for every note no path rule excludes.

    Frontmatter (`quoth: exclude`) is checked by the reader, which has the bytes."""
    rules = IgnoreRules.load(vault)
    for entry in walk(vault, rules):
        if not entry.excluded:
            yield entry.rel, entry.path


def read_note(path: Path) -> tuple[bytes, os.stat_result]:
    """The note's bytes and the stat of the very file they were read from."""
    with open(path, "rb") as f:
        st = os.fstat(f.fileno())
        return f.read(), st


def _drop_derived(conn: sqlite3.Connection, version_id: int) -> None:
    """Remove the rows kept only for current chunks: lexical indexes and links."""
    for tbl in ("chunks_fts", "chunks_fts_trigram"):
        conn.execute(
            f"DELETE FROM {tbl} WHERE rowid IN (SELECT id FROM chunks WHERE source_version_id = ?)",
            (version_id,),
        )
    conn.execute("DELETE FROM links WHERE chunk_id IN"
                 " (SELECT id FROM chunks WHERE source_version_id = ?)", (version_id,))


def _retire_version(conn: sqlite3.Connection, version_id: int) -> None:
    conn.execute("UPDATE source_versions SET is_current = 0 WHERE id = ?", (version_id,))
    _drop_derived(conn, version_id)


def _purge_sources(conn: sqlite3.Connection, source_ids: list[int]) -> None:
    """Delete every row of these notes, current and historical (they became excluded)."""
    if not source_ids:
        return
    conn.executemany("INSERT OR IGNORE INTO purge_sources(source_id) VALUES (?)",
                     [(s,) for s in source_ids])
    chunks = ("SELECT c.id FROM chunks c JOIN source_versions v ON v.id = c.source_version_id"
              " JOIN purge_sources p ON p.source_id = v.source_id")
    for tbl in ("chunks_fts", "chunks_fts_trigram"):
        conn.execute(f"DELETE FROM {tbl} WHERE rowid IN ({chunks})")
    for tbl in ("links", "entities", "embeddings"):
        conn.execute(f"DELETE FROM {tbl} WHERE chunk_id IN ({chunks})")
    conn.execute(f"DELETE FROM chunks WHERE id IN ({chunks})")
    conn.execute("DELETE FROM source_versions WHERE source_id IN (SELECT source_id FROM purge_sources)")
    conn.execute("DELETE FROM sources WHERE id IN (SELECT source_id FROM purge_sources)")
    conn.execute("DELETE FROM purge_sources")
    # Merge the FTS segments so no posting of a purged chunk survives in the index pages.
    for tbl in ("chunks_fts", "chunks_fts_trigram"):
        conn.execute(f"INSERT INTO {tbl}({tbl}) VALUES ('optimize')")


def _add_version(conn, source_id: int, rel: str, raw: bytes, digest: str,
                 st: os.stat_result) -> int:
    # utf-8-sig: a byte-order mark would otherwise hide the frontmatter fence.
    text = raw.decode("utf-8-sig", errors="replace").replace("\r\n", "\n")
    meta, chunks = chunk_markdown(text)
    links = extract_links(text)
    cur = conn.execute(
        "INSERT INTO source_versions(source_id, content_hash, mtime_ns, size_bytes,"
        " is_current, metadata) VALUES (?, ?, ?, ?, 1, ?)",
        (source_id, digest, st.st_mtime_ns, len(raw),
         json.dumps(meta, ensure_ascii=False, default=str)),
    )
    vid = cur.lastrowid
    title = Path(rel).stem
    for ch in chunks:
        cid = conn.execute(
            "INSERT INTO chunks(source_version_id, heading_path, line_start, line_end, text)"
            " VALUES (?, ?, ?, ?, ?)",
            (vid, ch.heading_path, ch.line_start, ch.line_end, ch.text),
        ).lastrowid
        for tbl in ("chunks_fts", "chunks_fts_trigram"):
            conn.execute(
                f"INSERT INTO {tbl}(rowid, title, heading_path, text) VALUES (?, ?, ?, ?)",
                (cid, title, ch.heading_path, ch.text),
            )
        conn.executemany(
            "INSERT INTO entities(chunk_id, entity, cnt) VALUES (?, ?, ?)",
            [(cid, e, n) for e, n in entities_for_chunk(title, ch.heading_path, ch.text).items()],
        )
        # Resolution is filled in by _resolve_links once the whole vault is known.
        conn.executemany(
            "INSERT INTO links(chunk_id, target, target_path, heading, line, kind, status)"
            " VALUES (?, ?, NULL, ?, ?, ?, 'missing')",
            [(cid, ln.target, ln.heading, ln.line, ln.kind) for ln in links
             if ch.line_start <= ln.line <= ch.line_end and ln.raw in ch.text],
        )
    return len(chunks)


def _resolve_links(conn: sqlite3.Connection, resolver: Resolver) -> None:
    rows = conn.execute(
        "SELECT l.rowid, l.kind, l.target, s.path, l.status, l.target_path FROM links l"
        " JOIN chunks c ON c.id = l.chunk_id"
        " JOIN source_versions v ON v.id = c.source_version_id AND v.is_current = 1"
        " JOIN sources s ON s.id = v.source_id").fetchall()
    updates = []
    for rowid, kind, target, source, status, target_path in rows:
        new = resolver.resolve(kind, target, source)
        if new != (status, target_path):
            updates.append((new[0], new[1], rowid))
    conn.executemany("UPDATE links SET status = ?, target_path = ? WHERE rowid = ?", updates)


def ingest_vault(conn: sqlite3.Connection, vault: str | Path) -> IngestStats:
    vault = Path(vault).resolve()
    if not vault.is_dir():
        raise FileNotFoundError(f"vault not found: {vault}")
    cfg = load_config()
    rules = IgnoreRules.load(vault)
    stats = IngestStats()

    existing: dict[str, tuple[int, str, int | None, str | None]] = {}
    for r in conn.execute(
        "SELECT s.id, s.path, s.scope, v.id AS vid, v.content_hash FROM sources s"
        " LEFT JOIN source_versions v ON v.source_id = s.id AND v.is_current = 1"
    ):
        existing[r["path"]] = (r["id"], r["scope"], r["vid"], r["content_hash"])

    seen: set[str] = set()
    excluded: set[str] = set()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for entry in walk(vault, rules):
            rel = entry.rel
            if entry.excluded:
                excluded.add(rel)
                continue
            try:
                raw, st = read_note(entry.path)
            except OSError as e:
                seen.add(rel)  # unreadable is not removed; the error fails a build
                stats.errors.append(f"{rel}: {e}")
                continue
            if frontmatter_excluded(raw):
                excluded.add(rel)
                continue
            seen.add(rel)
            digest = hashlib.sha256(raw).hexdigest()
            scope = cfg.scope_for(rel)
            row = existing.get(rel)
            if row is None:
                sid = conn.execute(
                    "INSERT INTO sources(path, scope) VALUES (?, ?)", (rel, scope)
                ).lastrowid
                stats.chunks_added += _add_version(conn, sid, rel, raw, digest, st)
                stats.new += 1
                continue
            sid, old_scope, vid, old_hash = row
            if old_scope != scope:  # scope config changed: re-derive, keeps privacy current
                conn.execute("UPDATE sources SET scope = ? WHERE id = ?", (scope, sid))
            if vid is not None and old_hash == digest:
                # Same content: keep the version, refresh its file facts.
                conn.execute("UPDATE source_versions SET mtime_ns = ?, size_bytes = ?"
                             " WHERE id = ? AND (mtime_ns != ? OR size_bytes != ?)",
                             (st.st_mtime_ns, len(raw), vid, st.st_mtime_ns, len(raw)))
                stats.unchanged += 1
                continue
            if vid is not None:
                _retire_version(conn, vid)
                stats.changed += 1
            else:
                stats.new += 1  # path re-appeared after removal
            stats.chunks_added += _add_version(conn, sid, rel, raw, digest, st)

        stats.excluded = len(excluded)
        # A note excluded now (by path, or by frontmatter while it exists) is purged with
        # its whole history; a note that was merely deleted keeps its retired history.
        purge = sorted({sid for rel, (sid, *_rest) in existing.items()
                       if rel in excluded or path_excluded(rel, rules)})
        _purge_sources(conn, purge)
        stats.purged = len(purge)
        purged_ids = set(purge)
        for rel, (sid, _scope, vid, _h) in existing.items():
            if rel not in seen and vid is not None and sid not in purged_ids:
                _retire_version(conn, vid)
                stats.removed += 1
        _resolve_links(conn, Resolver(seen, excluded))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return stats
