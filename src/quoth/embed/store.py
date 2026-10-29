"""Incremental embedding of current chunks into the `embeddings` table (BLOB float32)."""

from __future__ import annotations

import hashlib
import resource
import sqlite3
import sys
import time
from dataclasses import dataclass

import numpy as np

from ..chunker import passage_text
from .backends import Embedder

_PENDING = (
    " FROM chunks c"
    " JOIN source_versions v ON v.id = c.source_version_id AND v.is_current = 1"
    " JOIN sources s ON s.id = v.source_id"
    " LEFT JOIN embeddings e ON e.chunk_id = c.id AND e.model = ?"
    " WHERE e.chunk_id IS NULL"
)


def to_blob(v: np.ndarray) -> bytes:
    return np.asarray(v, dtype="<f4").tobytes()


def from_blob(b: bytes, dim: int) -> np.ndarray:
    a = np.frombuffer(b, dtype="<f4")
    if a.shape[0] != dim:
        raise ValueError(f"vector length {a.shape[0]} != dim {dim}")
    return a


def peak_rss_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024.0 * 1024.0) if sys.platform == "darwin" else peak / 1024.0  # bytes on macOS, KiB on Linux


@dataclass
class EmbedStats:
    model: str
    embedded: int = 0
    reused: int = 0
    skipped_existing: int = 0
    batches: int = 0
    seconds: float = 0.0
    chunks_per_second: float = 0.0
    peak_rss_mb: float = 0.0

    def as_dict(self) -> dict:
        return dict(self.__dict__)


def pending_count(conn: sqlite3.Connection, model: str) -> int:
    return conn.execute("SELECT count(*)" + _PENDING, (model,)).fetchone()[0]


def _passage_key(path: str, heading_path: str, text: str, metadata: str | None) -> bytes:
    return hashlib.sha256(passage_text(path, heading_path, text, metadata).encode()).digest()


def reusable_vectors(conn: sqlite3.Connection, model: str, dim: int) -> dict[bytes, int]:
    """Map the exact embedder input of every stored vector (any version) to its chunk_id.

    A new chunk whose passage text is identical to an already embedded one gets
    that vector copied instead of re-encoded: same model, same input, same vector.
    """
    rows = conn.execute(
        "SELECT e.chunk_id, s.path, c.heading_path, c.text, v.metadata FROM embeddings e"
        " JOIN chunks c ON c.id = e.chunk_id"
        " JOIN source_versions v ON v.id = c.source_version_id"
        " JOIN sources s ON s.id = v.source_id"
        " WHERE e.model = ? AND e.dim = ? AND length(e.vector) = ? ORDER BY e.chunk_id",
        (model, dim, 4 * dim))
    out: dict[bytes, int] = {}
    for chunk_id, path, heading_path, text, metadata in rows:
        out.setdefault(_passage_key(path, heading_path, text, metadata), chunk_id)
    return out


def embed_chunks(conn: sqlite3.Connection, embedder: Embedder, batch_size: int = 64,
                 limit: int | None = None, progress=None, reuse: bool = True) -> EmbedStats:
    """Embed current chunks that have no vector for this model yet. Idempotent; commits per batch.

    With reuse, chunks whose passage text already has a stored vector copy it.
    """
    spec = embedder.spec
    st = EmbedStats(model=spec.name)
    total_current = conn.execute(
        "SELECT count(*) FROM chunks c JOIN source_versions v"
        " ON v.id = c.source_version_id AND v.is_current = 1").fetchone()[0]
    sql = "SELECT c.id, s.path, c.heading_path, c.text, v.metadata" + _PENDING + " ORDER BY c.id"
    rows = conn.execute(sql + (" LIMIT ?" if limit else ""),
                        (spec.name, limit) if limit else (spec.name,)).fetchall()
    st.skipped_existing = total_current - pending_count(conn, spec.name)
    t0 = time.monotonic()
    if reuse and rows:
        known = reusable_vectors(conn, spec.name, spec.dim)
        copies = [(r[0], known[k]) for r in rows
                  if (k := _passage_key(r[1], r[2], r[3], r[4])) in known]
        if copies:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.executemany(
                    "INSERT OR IGNORE INTO embeddings(chunk_id, model, dim, vector)"
                    " SELECT ?, model, dim, vector FROM embeddings WHERE chunk_id = ? AND model = ?",
                    [(new, old, spec.name) for new, old in copies])
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            st.reused = len(copies)
            copied = {new for new, _ in copies}
            rows = [r for r in rows if r[0] not in copied]
    # Shortest first: batches of similar length waste little compute on padding.
    todo = sorted(((r[0], passage_text(r[1], r[2], r[3], r[4])) for r in rows),
                  key=lambda item: (len(item[1]), item[0]))
    for i in range(0, len(todo), batch_size):
        batch = todo[i:i + batch_size]
        vecs = embedder.embed_passages([text for _, text in batch])
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany(
                "INSERT OR IGNORE INTO embeddings(chunk_id, model, dim, vector) VALUES (?, ?, ?, ?)",
                [(cid, spec.name, spec.dim, to_blob(v)) for (cid, _), v in zip(batch, vecs)])
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        st.embedded += len(batch)
        st.batches += 1
        if progress:
            progress(st.embedded, len(todo), time.monotonic() - t0)
    st.seconds = round(time.monotonic() - t0, 3)
    st.chunks_per_second = round(st.embedded / st.seconds, 1) if st.seconds else 0.0
    st.peak_rss_mb = round(peak_rss_mb(), 1)
    return st


def load_matrix(conn: sqlite3.Connection, model: str, scopes: tuple[str, ...]):
    """Return (chunk_ids int64[N], matrix float32[N, dim]) for current chunks in scopes."""
    scope_clause = f" AND s.scope IN ({','.join('?' * len(scopes))})" if scopes else ""
    rows = conn.execute(
        "SELECT e.chunk_id, e.dim, e.vector FROM embeddings e"
        " JOIN chunks c ON c.id = e.chunk_id"
        " JOIN source_versions v ON v.id = c.source_version_id AND v.is_current = 1"
        " JOIN sources s ON s.id = v.source_id"
        f" WHERE e.model = ?{scope_clause} ORDER BY e.chunk_id",
        (model, *scopes)).fetchall()
    if not rows:
        return np.zeros(0, dtype=np.int64), np.zeros((0, 0), dtype=np.float32)
    dim = rows[0][1]
    ids = np.fromiter((r[0] for r in rows), dtype=np.int64, count=len(rows))
    mat = np.frombuffer(b"".join(r[2] for r in rows), dtype="<f4").reshape(len(rows), dim)
    return ids, mat
