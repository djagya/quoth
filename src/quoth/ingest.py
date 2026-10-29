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
from .scopes import load_config


@dataclass
class IngestStats:
    new: int = 0
    changed: int = 0
    unchanged: int = 0
    removed: int = 0
    chunks_added: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "new": self.new, "changed": self.changed, "unchanged": self.unchanged,
            "removed": self.removed, "chunks_added": self.chunks_added, "errors": self.errors,
        }


def iter_vault_notes(vault: Path):
    """Yield (rel_posix_path, abs_path) for every note outside a skipped directory."""
    cfg = load_config()
    for root, dirs, fnames in os.walk(vault, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not cfg.skip_dir(d))
        for f in sorted(fnames):
            if cfg.is_note(f):
                p = Path(root) / f
                if p.is_file():
                    yield p.relative_to(vault).as_posix(), p


def _drop_from_fts(conn: sqlite3.Connection, version_id: int) -> None:
    for tbl in ("chunks_fts", "chunks_fts_trigram"):
        conn.execute(
            f"DELETE FROM {tbl} WHERE rowid IN (SELECT id FROM chunks WHERE source_version_id = ?)",
            (version_id,),
        )


def _retire_version(conn: sqlite3.Connection, version_id: int) -> None:
    conn.execute("UPDATE source_versions SET is_current = 0 WHERE id = ?", (version_id,))
    _drop_from_fts(conn, version_id)


def _add_version(conn, source_id: int, rel: str, raw: bytes, digest: str) -> int:
    # utf-8-sig: a byte-order mark would otherwise hide the frontmatter fence.
    text = raw.decode("utf-8-sig", errors="replace").replace("\r\n", "\n")
    meta, chunks = chunk_markdown(text)
    cur = conn.execute(
        "INSERT INTO source_versions(source_id, content_hash, is_current, metadata)"
        " VALUES (?, ?, 1, ?)",
        (source_id, digest, json.dumps(meta, ensure_ascii=False, default=str)),
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
    return len(chunks)


def ingest_vault(conn: sqlite3.Connection, vault: str | Path) -> IngestStats:
    vault = Path(vault).resolve()
    if not vault.is_dir():
        raise FileNotFoundError(f"vault not found: {vault}")
    cfg = load_config()
    stats = IngestStats()

    existing: dict[str, tuple[int, str, int | None, str | None]] = {}
    for r in conn.execute(
        "SELECT s.id, s.path, s.scope, v.id AS vid, v.content_hash FROM sources s"
        " LEFT JOIN source_versions v ON v.source_id = s.id AND v.is_current = 1"
    ):
        existing[r["path"]] = (r["id"], r["scope"], r["vid"], r["content_hash"])

    seen: set[str] = set()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for rel, p in iter_vault_notes(vault):
            seen.add(rel)
            try:
                raw = p.read_bytes()
            except OSError as e:
                stats.errors.append(f"{rel}: {e}")
                continue
            digest = hashlib.sha256(raw).hexdigest()
            scope = cfg.scope_for(rel)
            row = existing.get(rel)
            if row is None:
                sid = conn.execute(
                    "INSERT INTO sources(path, scope) VALUES (?, ?)", (rel, scope)
                ).lastrowid
                stats.chunks_added += _add_version(conn, sid, rel, raw, digest)
                stats.new += 1
                continue
            sid, old_scope, vid, old_hash = row
            if old_scope != scope:  # scope config changed: re-derive, keeps privacy current
                conn.execute("UPDATE sources SET scope = ? WHERE id = ?", (scope, sid))
            if vid is not None and old_hash == digest:
                stats.unchanged += 1
                continue
            if vid is not None:
                _retire_version(conn, vid)
                stats.changed += 1
            else:
                stats.new += 1  # path re-appeared after removal
            stats.chunks_added += _add_version(conn, sid, rel, raw, digest)

        for rel, (sid, _scope, vid, _h) in existing.items():
            if rel not in seen and vid is not None:
                _retire_version(conn, vid)
                stats.removed += 1
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return stats
