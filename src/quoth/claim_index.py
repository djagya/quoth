"""Dense index over accepted ledger claims, so recall matches meaning, not only shared words.

Vectors live in memory only (claims are few and short); they are derived data, rebuilt
after a restart by the sidecar's background sync, a bounded batch at a time so serving
is never starved. A claim's vector is keyed by its text: an edit is a new claim id anyway.
"""
from __future__ import annotations

import threading

import numpy as np

# Cosine floor for a claim to count as relevant to a turn (bge-m3, normalised vectors).
MIN_SIMILARITY = 0.5
SYNC_BATCH = 32


class ClaimIndex:
    def __init__(self, min_similarity: float = MIN_SIMILARITY):
        self.min_similarity = min_similarity
        self._vectors: dict[int, tuple[str, np.ndarray]] = {}
        self._matrix: tuple[np.ndarray, np.ndarray] | None = None
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._vectors)

    def known(self, claims: list[tuple[int, str]]) -> dict[int, np.ndarray]:
        """Vectors already held for these (id, text) pairs, text unchanged."""
        with self._lock:
            return {cid: self._vectors[cid][1] for cid, text in claims
                    if cid in self._vectors and self._vectors[cid][0] == text}

    def sync(self, claims: list[tuple[int, str]], embedder, batch: int = SYNC_BATCH,
             reuse: "ClaimIndex | None" = None) -> int:
        """Drop vectors of claims no longer given; embed up to `batch` missing ones. Returns embedded.

        Vectors `reuse` already holds for the same text are copied, not embedded again.
        """
        wanted = dict(claims)
        with self._lock:
            stale = [cid for cid, (text, _) in self._vectors.items() if wanted.get(cid) != text]
            for cid in stale:
                del self._vectors[cid]
            missing = [(cid, text) for cid, text in claims if cid not in self._vectors]
            if stale:
                self._matrix = None
        if reuse is not None and missing:
            borrowed = reuse.known(missing)
            if borrowed:
                with self._lock:
                    for cid, vec in borrowed.items():
                        self._vectors[cid] = (wanted[cid], vec)
                    self._matrix = None
                missing = [(cid, text) for cid, text in missing if cid not in borrowed]
        missing = missing[:batch]
        if missing:
            vecs = np.asarray(embedder.embed_passages([text for _, text in missing]), dtype=np.float32)
            with self._lock:
                for (cid, text), vec in zip(missing, vecs):
                    if wanted.get(cid) == text:
                        self._vectors[cid] = (text, vec)
                self._matrix = None
        return len(missing)

    def text(self, claim_id: int) -> str | None:
        """The indexed text of a claim (what its vector was made from), or None."""
        with self._lock:
            hit = self._vectors.get(claim_id)
        return hit[0] if hit is not None else None

    def matrix(self) -> tuple[np.ndarray, np.ndarray]:
        """(ids ascending, row-aligned vectors) of every embedded claim."""
        with self._lock:
            if self._matrix is None:
                ids = np.array(sorted(self._vectors), dtype=np.int64)
                mat = (np.stack([self._vectors[int(i)][1] for i in ids]) if ids.size
                       else np.zeros((0, 0), dtype=np.float32))
                self._matrix = (ids, mat)
            return self._matrix

    def rank(self, query_vector: np.ndarray, k: int) -> list[tuple[int, float]]:
        """[(claim_id, cosine)] best first, at or above the similarity floor; ties by id."""
        ids, mat = self.matrix()
        if ids.size == 0 or k <= 0:
            return []
        sims = mat @ np.asarray(query_vector, dtype=np.float32)
        order = np.lexsort((ids, -sims))
        return [(int(ids[i]), float(sims[i])) for i in order[:k] if sims[i] >= self.min_similarity]
