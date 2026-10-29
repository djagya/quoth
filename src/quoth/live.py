"""Per-hit freshness: is the note a hit came from still what the snapshot indexed?

A hit's note is compared with the live vault file: a stat first; a hash only when the
size matches but the mtime does not (or the snapshot recorded neither). Each hit gets
``live``: ``same``, ``changed`` (the live note differs from the indexed one) or
``missing`` (no longer in the vault). The cost per call is capped (`recall_defaults`:
LIVE_MAX_STATS stats, LIVE_MAX_HASHES hashes, files up to LIVE_HASH_MAX_BYTES); a hit past
a cap, or whose file cannot be read, has no ``live`` field: unknown, never guessed.
The vault is only read.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import time

from . import recall_defaults as D

FACTS_KEY = "_indexed"  # the hit's `snapshot_contract.IndexedFile`, removed by `check`


class LiveCheck:
    def __init__(self, root: str | Path, *, max_stats: int = D.LIVE_MAX_STATS,
                 max_hashes: int = D.LIVE_MAX_HASHES, max_bytes: int = D.LIVE_HASH_MAX_BYTES):
        self.root = Path(root).resolve()
        self.max_stats, self.max_hashes, self.max_bytes = max_stats, max_hashes, max_bytes

    def check(self, hits: list[dict]) -> dict:
        """Set ``live`` on each hit it can check (in order); returns the cost
        ``{stats, hashes, unchecked, ms}``. Hits of one note share one check."""
        started = time.monotonic()
        stats = hashes = unchecked = 0
        known: dict[str, str | None] = {}
        for hit in hits:
            facts = hit.pop(FACTS_KEY, None)
            path = hit.get("path")
            if facts is None or not isinstance(path, str):
                unchecked += 1
                continue
            if path not in known:
                if stats >= self.max_stats:
                    unchecked += 1
                    continue
                stats += 1
                status, hashed = self._one(path, facts, hashes < self.max_hashes)
                hashes += hashed
                known[path] = status
            if known[path] is None:
                unchecked += 1
            else:
                hit["live"] = known[path]
        return {"stats": stats, "hashes": hashes, "unchecked": unchecked,
                "ms": round((time.monotonic() - started) * 1000, 2)}

    def _one(self, path: str, facts, may_hash: bool) -> tuple[str | None, int]:
        parts = path.split("/")
        if path.startswith("/") or any(p in ("", ".", "..") for p in parts):
            return None, 0
        full = self.root.joinpath(*parts)
        try:
            st = os.stat(full)
        except (FileNotFoundError, NotADirectoryError):
            return "missing", 0
        except OSError:
            return None, 0
        if facts.size is not None and st.st_size != facts.size:
            return "changed", 0
        if facts.size is not None and facts.mtime_ns is not None and st.st_mtime_ns == facts.mtime_ns:
            return "same", 0
        if not may_hash or st.st_size > self.max_bytes:
            return None, 0
        try:
            with open(full, "rb") as fh:
                digest = hashlib.sha256(fh.read(self.max_bytes + 1)).hexdigest()
        except OSError:
            return None, 1
        return ("same" if digest == facts.sha256 else "changed"), 1
