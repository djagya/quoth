"""Read-only serving caches for the admitted evidence snapshot.

The read-only MCP path serves an admitted, immutable snapshot, so per-call
signature queries (dense matrix, date index, vector shape) are pure overhead.
Per-connection state is loaded once and keyed by the connection object; it is
dropped when ServingSnapshot.lease swaps the pointer or the snapshot closes,
and reset if `PRAGMA data_version` ever reports a foreign commit.

Ranking is identical to dense.dense_topk: same load_matrix rows, same
`mat @ q`, argpartition then lexsort by (-sim, chunk_id).
"""
from __future__ import annotations

from collections import OrderedDict
import threading

import numpy as np

from .cpu import embed_threads
from .dated import date_index
from .embed.backends import get_embedder
from .embed.store import load_matrix
from .recall_defaults import QUERY_EMBED_WAIT

_MAX_CONNS = 8
_STATE: dict = {}
_STATE_LOCK = threading.Lock()


class ConnState:
    """Snapshot-lifetime state for one admitted connection; use under its lease."""

    def __init__(self, conn, version):
        self.conn = conn
        self.version = version
        self.vectors_ok: set[str] = set()
        self._matrices: dict = {}
        self._dates = None

    def matrix(self, model: str):
        """(ids, mat) of all current vectors for `model`: dense._matrix with scopes=()."""
        hit = self._matrices.get(model)
        if hit is None:
            hit = self._matrices[model] = load_matrix(self.conn, model, ())
        return hit

    def dates(self):
        if self._dates is None:
            self._dates = date_index(self.conn)
        return self._dates


def state(conn) -> ConnState:
    """Cached state for `conn`, reset if another connection ever committed to its DB."""
    version = conn.execute("PRAGMA data_version").fetchone()[0]
    with _STATE_LOCK:
        st = _STATE.get(conn)
        if st is None or st.version != version:
            if conn not in _STATE and len(_STATE) >= _MAX_CONNS:
                _STATE.pop(next(iter(_STATE)))
            st = _STATE[conn] = ConnState(conn, version)
        return st


def drop_cache(conn) -> None:
    with _STATE_LOCK:
        _STATE.pop(conn, None)


def rank(ids: np.ndarray, mat: np.ndarray, q: np.ndarray, limit: int) -> list[tuple[int, float]]:
    """dense.dense_topk's scoring and tie-break on a preloaded matrix."""
    if ids.size == 0:
        return []
    sims = mat @ q
    n = min(limit, sims.shape[0])
    top = np.argpartition(-sims, n - 1)[:n]
    top = top[np.lexsort((ids[top], -sims[top]))]
    return [(int(ids[i]), float(sims[i])) for i in top]


class QueryEmbedder:
    """Per-server embedders (loaded once, serialized per model) and a bounded query-vector LRU.

    The LRU is single-flight per (model, query): when a turn's lanes and its evidence job ask
    for the same cold query at once, one embeds and the others wait for its vector, so a turn
    embeds its query once. A waiter that gives up (`wait` seconds) or whose leader failed
    embeds the query itself.
    """

    def __init__(self, maxsize: int = 256, wait: float = QUERY_EMBED_WAIT):
        self.maxsize = maxsize
        self.wait = wait
        self._embedders: dict = {}
        self._locks: dict = {}
        self._lock = threading.Lock()
        self._vectors: OrderedDict = OrderedDict()
        self._flights: dict = {}  # (model, query) -> Event set when the leader's embed ends

    def embedder(self, model: str):
        # Failures are never cached: every call retries and raises as before.
        hit = self._embedders.get(model)
        if hit is not None:
            return hit
        with self._lock:
            lock = self._locks.setdefault(model, threading.Lock())
        with lock:
            hit = self._embedders.get(model)
            if hit is None:
                hit = self._embedders[model] = get_embedder(model, threads=embed_threads())
            return hit

    def seed(self, model: str, embedder) -> None:
        """Use an already loaded embedder for `model` (eval harness: one model per process)."""
        with self._lock:
            self._embedders[model] = embedder

    def vector(self, model: str, embedder, query: str, *, cache: bool = True) -> np.ndarray:
        """Query vector, LRU-cached and single-flight; `cache=False` (bulk callers) neither
        reads nor fills the LRU."""
        if not cache:
            return np.array(embedder.embed_query(query), dtype=np.float32)
        key = (model, query)
        with self._lock:
            hit = self._get(key)
            if hit is not None:
                return hit
            flight = self._flights.get(key)
            leader = flight is None
            if leader:
                flight = self._flights[key] = threading.Event()
        if not leader:
            if flight.wait(self.wait):
                with self._lock:
                    hit = self._get(key)
                if hit is not None:
                    return hit
            return self._embed(key, embedder, query)  # the leader is late or failed
        try:
            return self._embed(key, embedder, query)
        finally:
            with self._lock:
                if self._flights.get(key) is flight:
                    del self._flights[key]
            flight.set()

    def _get(self, key):
        """Cached vector or None; call under `_lock`."""
        hit = self._vectors.get(key)
        if hit is not None:
            self._vectors.move_to_end(key)
        return hit

    def _embed(self, key, embedder, query: str) -> np.ndarray:
        q = np.array(embedder.embed_query(query), dtype=np.float32)
        q.flags.writeable = False
        with self._lock:
            self._vectors[key] = q
            self._vectors.move_to_end(key)
            while len(self._vectors) > self.maxsize:
                self._vectors.popitem(last=False)
        return q

    def topk(self, st: ConnState, model: str, embedder, query: str,
             limit: int, *, cache: bool = True) -> list[tuple[int, float]]:
        if not query.strip() or limit <= 0:
            return []
        ids, mat = st.matrix(model)
        if ids.size == 0:
            return []
        return rank(ids, mat, self.vector(model, embedder, query, cache=cache), limit)
