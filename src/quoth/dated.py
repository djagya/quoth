"""Validated note-writing dates on unchanged dense evidence (T1 metadata salvage).

A date is accepted only when the valid calendar filename prefix equals stored
frontmatter created's date. Never infer an event date, mtime or ingestion date.
"""
from __future__ import annotations

import json
import re
from datetime import date

from .evidence import evidence_bundle
from .search import RRF_K, dense_rows, search

_FN = re.compile(r"^(\d{4}-\d{2}-\d{2})")
_DATE_CACHE: dict = {}


def note_date(path: str, metadata: str | None) -> tuple[str | None, str]:
    """Return (validated ISO note_date or None, date_basis) from path + frontmatter."""
    m = _FN.match(str(path).rsplit("/", 1)[-1])
    if not m:
        return None, "none"
    try:
        date.fromisoformat(m.group(1))
    except ValueError:
        return None, "none"
    try:
        meta = json.loads(metadata) if metadata else {}
    except (TypeError, ValueError):
        meta = {}
    created = str((meta if isinstance(meta, dict) else {}).get("created") or "")[:10]
    if created == m.group(1):
        return created, "validated_note_date"
    return None, "filename_only"


class DateIndex:
    """Current per-snapshot date metadata only; no chronological candidate index."""

    def __init__(self, by_path: dict):
        self.by_path = by_path

    def annotate(self, hits: list[dict]) -> list[dict]:
        for h in hits:
            h["note_date"], h["date_basis"] = self.by_path.get(h["path"], (None, "none"))
        return hits


def _signature(conn):
    # Own writes and writes through another connection invalidate metadata edits too.
    return (conn.total_changes, conn.execute("PRAGMA data_version").fetchone()[0],
            *conn.execute(
                "SELECT (SELECT coalesce(max(id), 0) FROM source_versions),"
                " (SELECT count(*) FROM source_versions WHERE is_current = 1)").fetchone())


def date_index(conn) -> DateIndex:
    # Keep the actual connection in a bounded cache, not its reusable Python id.
    key, sig = conn, _signature(conn)
    hit = _DATE_CACHE.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    by_path = {}
    for path, metadata in conn.execute(
            "SELECT s.path, v.metadata FROM sources s"
            " JOIN source_versions v ON v.source_id = s.id AND v.is_current = 1"):
        by_path[path] = note_date(path, metadata)
    index = DateIndex(by_path)
    if key not in _DATE_CACHE and len(_DATE_CACHE) >= 8:
        _DATE_CACHE.pop(next(iter(_DATE_CACHE)))
    _DATE_CACHE[key] = (sig, index)
    return index


def drop_cache(conn) -> None:
    _DATE_CACHE.pop(conn, None)


def build_evidence(conn, query, k=10, model="bge-m3", embedder=None, *,
                   ranked=None, dates=None) -> dict:
    """Shared MCP/eval route: unchanged dense top-k, plus per-hit date metadata.

    `ranked` ([(chunk_id, cosine)] best-first, exactly dense.dense_topk's output for
    this query/k) and `dates` (date_index(conn)) let the read-only server reuse its
    snapshot caches; omitted, both are computed here as before.
    """
    if ranked is None:
        hits = search(conn, query, k=k, retriever="dense", model=model, embedder=embedder)
    elif k <= 0:
        hits = []
    else:
        # Same rows and scores as search._search's dense branch.
        rows = dense_rows(conn, query, k, model, embedder, ranked=ranked)
        hits = [{**r, "score": round(1 / (RRF_K + rank), 6), "retriever": "dense"}
                for rank, r in enumerate(rows, 1)]
    return evidence_bundle((date_index(conn) if dates is None else dates).annotate(hits), k=k)
