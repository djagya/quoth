"""Dense retriever: brute-force cosine with numpy over `embeddings` BLOBs (no sqlite-vec)."""

from __future__ import annotations

import sqlite3

import numpy as np

from .embed.backends import get_embedder
from .embed.store import load_matrix

_MATRIX_CACHE: dict = {}
_EMBEDDER_CACHE: dict = {}


def _matrix(conn: sqlite3.Connection, model: str, scopes: tuple[str, ...]):
    # Invalidate when vectors or current versions change.
    sig = conn.execute(
        "SELECT (SELECT count(*) FROM embeddings WHERE model = ?),"
        " (SELECT coalesce(max(id), 0) FROM source_versions),"
        " (SELECT count(*) FROM source_versions WHERE is_current = 1)", (model,)).fetchone()
    key = (id(conn), model, scopes)
    hit = _MATRIX_CACHE.get(key)
    if hit and hit[0] == tuple(sig):
        return hit[1], hit[2]
    ids, mat = load_matrix(conn, model, scopes)
    _MATRIX_CACHE[key] = (tuple(sig), ids, mat)
    return ids, mat


def dense_topk(conn: sqlite3.Connection, query: str, scopes: tuple[str, ...], limit: int,
               model: str, embedder=None) -> list[tuple[int, float]]:
    """Return [(chunk_id, cosine)] best-first. Vectors are stored L2-normalised, so dot = cosine."""
    if not query.strip() or limit <= 0:
        return []
    ids, mat = _matrix(conn, model, scopes)
    if ids.size == 0:
        return []
    if embedder is None:
        embedder = _EMBEDDER_CACHE.get(model)
        if embedder is None:
            embedder = _EMBEDDER_CACHE[model] = get_embedder(model)
    q = np.asarray(embedder.embed_query(query), dtype=np.float32)
    sims = mat @ q
    n = min(limit, sims.shape[0])
    top = np.argpartition(-sims, n - 1)[:n]
    top = top[np.lexsort((ids[top], -sims[top]))]
    return [(int(ids[i]), float(sims[i])) for i in top]
