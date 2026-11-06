"""Claims gold: resolve ledger-stable claim anchors against the target ledger at run time.

A row names an expected claim by an anchor, `{"store": "claims", "match": {"claim": {"text",
"id"?, "subject_key"?}}}`: the claim's full text when it was labelled, optionally its id in
the ledger it was labelled on and its subject_key. Claim ids are per ledger and an edit makes
a new claim, so the anchor is resolved at run time, never trusted as is:

1. **Find the labelled claim.** By `id` when that claim's text still equals the anchor's
   (`ledger.core.normalize`: case, spacing and final punctuation aside), else by text over
   every claim of the ledger (nothing is ever deleted, so an edited or superseded claim is
   still there). With neither, the one accepted current claim of `subject_key`.
2. **Follow it forward** through `superseded_by` (an owner's edit or a supersede) to the
   claim that holds the slot now. That claim must be accepted and current: the anchor then
   becomes `{"locator": "claim:<id>"}` for this run. Stale anchors are not followed (the
   stale value is that exact claim).
3. **Otherwise it is unresolved**, with a reason: `not_found`, `ambiguous` (several current
   claims), `pending`, `rejected`, `expired`, `forgotten`, or `no_ledger` (a socket run
   without `--ledger`). Unresolved gold is removed from the row and reported in the receipt's
   `claims_gold`; a row left with no expected source is not scored at all, so an unresolved
   anchor is never counted as a miss.

Reports hold counts and row ids only, never claim text.
"""
from __future__ import annotations

from collections import Counter
import sqlite3
from pathlib import Path
from urllib.parse import quote

from ..ledger.core import normalize
from .format import claim_anchors

REASONS = ("not_found", "ambiguous", "pending", "rejected", "expired", "forgotten", "no_ledger")
# When several labelled copies end unresolved, the reason reported (most actionable first).
_REASON_ORDER = ("pending", "forgotten", "rejected", "expired")
MAX_CHAIN = 1000


class Resolver:
    """Anchor resolution over one ledger connection (read-only use)."""

    def __init__(self, conn: sqlite3.Connection | None, lock=None, owns: bool = False):
        self.conn = conn
        self.lock = lock
        self.owns = owns
        self._by_text: dict[str, list[int]] | None = None

    @classmethod
    def open(cls, path: str | Path) -> "Resolver":
        """A read-only connection to a ledger file (the live one may be open by the sidecar)."""
        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError("ledger not found")
        conn = sqlite3.connect(f"file:{quote(str(p.resolve()))}?mode=ro", uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("SELECT id, text, superseded_by FROM claims LIMIT 1").fetchall()
        except sqlite3.Error:
            conn.close()
            raise
        return cls(conn, owns=True)

    def close(self) -> None:
        if self.owns and self.conn is not None:
            self.conn.close()

    def _row(self, claim_id: int):
        return self.conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()

    def _ids_by_text(self, text: str) -> list[int]:
        if self._by_text is None:
            self._by_text = {}
            for cid, t in self.conn.execute("SELECT id, text FROM claims ORDER BY id"):
                self._by_text.setdefault(normalize(t), []).append(cid)
        return self._by_text.get(normalize(text), [])

    @staticmethod
    def _state(row) -> str:
        if row["superseded_by"] is not None:
            return "superseded"
        if row["review_status"] == "accepted":
            return "current" if row["valid_to"] is None else "forgotten"
        return row["review_status"]  # pending | rejected | expired

    def _follow(self, claim_id: int) -> tuple[sqlite3.Row, Counter]:
        """The claim at the end of `claim_id`'s supersede chain, and its steps by kind."""
        steps, seen = Counter(), {claim_id}
        row = self._row(claim_id)
        while row["superseded_by"] is not None and len(seen) < MAX_CHAIN:
            nxt = row["superseded_by"]
            if nxt in seen:
                break  # a cycle never happens in a sound ledger; stop rather than loop
            seen.add(nxt)
            edited = self.conn.execute("SELECT 1 FROM ledger_events WHERE claim_id = ? AND action = 'edited_from'"
                                       " LIMIT 1", (nxt,)).fetchone()
            steps["edit" if edited else "supersede"] += 1
            row = self._row(nxt)
        return row, steps

    def resolve(self, anchor: dict, follow: bool = True) -> dict:
        """{"ids": [claim id, …] (one when followed), "how": id|text|subject_key, "steps":
        {edit, supersede}} when resolved, else {"ids": [], "reason": code}."""
        if self.conn is None:
            return {"ids": [], "reason": "no_ledger"}
        if self.lock is not None:
            with self.lock:
                return self._resolve(anchor, follow)
        return self._resolve(anchor, follow)

    def _resolve(self, anchor: dict, follow: bool) -> dict:
        starts, how = [], None
        if anchor.get("id") is not None:
            row = self._row(anchor["id"])
            if row is not None and normalize(row["text"]) == normalize(anchor["text"]):
                starts, how = [anchor["id"]], "id"
        if not starts:
            starts = self._ids_by_text(anchor["text"])
            how = "text" if starts else None
        if not starts and anchor.get("subject_key"):
            current = [r["id"] for r in self.conn.execute(
                "SELECT * FROM claims WHERE subject_key = ? AND review_status = 'accepted' AND valid_to IS NULL"
                " AND superseded_by IS NULL ORDER BY id", (anchor["subject_key"],))]
            if len(current) == 1:
                return {"ids": current, "how": "subject_key", "steps": {}}
            return {"ids": [], "reason": "ambiguous" if current else "not_found"}
        if not starts:
            return {"ids": [], "reason": "not_found"}
        if not follow:
            return {"ids": sorted(starts), "how": how, "steps": {}}
        ends, steps = {}, Counter()
        for cid in starts:
            row, walked = self._follow(cid)
            ends[row["id"]] = self._state(row)
            steps += walked
        current = sorted(cid for cid, state in ends.items() if state == "current")
        if len(current) == 1:
            return {"ids": current, "how": how, "steps": dict(steps)}
        if current:
            return {"ids": [], "reason": "ambiguous"}
        states = set(ends.values())
        return {"ids": [], "reason": next((r for r in _REASON_ORDER if r in states), "not_found")}


def _locator(cid: int) -> dict:
    return {"store": "claims", "match": {"locator": f"claim:{cid}"}}


def has_anchors(rows: list[dict]) -> bool:
    return any(claim_anchors(r) for r in rows)


def resolve_rows(rows: list[dict], resolver: Resolver | None) -> tuple[dict, dict | None]:
    """({row id: the row with its anchors resolved, or None when nothing is left to score},
    the receipt's `claims_gold` report). Rows without anchors are not in the mapping; with
    no anchor in any row the report is None."""
    if not has_anchors(rows):
        return {}, None
    resolver = resolver or Resolver(None)
    cache: dict = {}

    def lookup(anchor: dict, follow: bool) -> dict:
        key = (anchor.get("text"), anchor.get("id"), anchor.get("subject_key"), follow)
        if key not in cache:
            cache[key] = resolver.resolve(anchor, follow)
        return cache[key]

    out, how, steps, reasons, stale = {}, Counter(), Counter(), Counter(), Counter()
    excluded, partial = [], []
    for row in rows:
        if not claim_anchors(row):
            continue
        expect = row["expect"]
        lost = False

        def rewrite(src: dict, follow: bool = True, count: bool = True) -> list[dict]:
            nonlocal lost
            if src["store"] != "claims" or "claim" not in src["match"]:
                return [src]
            got = lookup(src["match"]["claim"], follow)
            if count:
                if got["ids"]:
                    how[got["how"]] += 1
                    steps.update(got.get("steps") or {})
                else:
                    reasons[got["reason"]] += 1
            if not got["ids"]:
                lost = lost or count
                return []
            return [_locator(cid) for cid in got["ids"]]

        sources = [s for src in expect["sources"] for s in rewrite(src)]
        claims_lost = lost
        new = {**expect, "sources": sources}
        if expect.get("negatives"):
            new["negatives"] = [s for src in expect["negatives"] for s in rewrite(src)]
        if expect.get("stale"):
            kept = []
            for src in expect["stale"]:
                got = rewrite(src, follow=False, count=False)
                if src["store"] == "claims" and "claim" in src["match"]:
                    stale["resolved" if got else "unresolved"] += 1
                kept += got
            new["stale"] = kept
        if expect.get("groups"):
            groups = [{"name": g["name"], "any_of": [s for src in g["any_of"] for s in rewrite(src, count=False)]}
                      for g in expect["groups"]]
            new["groups"] = [g for g in groups if g["any_of"]] or None
            if new["groups"] is None:
                del new["groups"]
        if claims_lost:
            # Part of the claims gold is unknown here: injected claims cannot be judged.
            new["claims_judged"] = False
            if sources or expect["abstain"]:
                partial.append(row["id"])
            else:
                excluded.append(row["id"])
                out[row["id"]] = None
                continue
        out[row["id"]] = {**row, "expect": new}
    report = {"anchors": sum(how.values()) + sum(reasons.values()), "resolved": sum(how.values()),
              "by": dict(sorted(how.items())), "followed": {"edit": steps.get("edit", 0),
                                                            "supersede": steps.get("supersede", 0)},
              "unresolved": dict(sorted(reasons.items())), "rows_excluded": excluded, "rows_partial": partial,
              "ledger": resolver.conn is not None}
    if stale:
        report["stale"] = {"resolved": stale.get("resolved", 0), "unresolved": stale.get("unresolved", 0)}
    return out, report
