"""Mutable core-memory ledger: typed claims, write-time reconcile, review, expiry, health.

The ledger lives in its own SQLite file beside the immutable evidence snapshot and
never touches it. Every claim records where its content comes from (`origin`): the
owner's own words (`owner_statement`, quote-verified against Hermes state.db), the
owner's notes (`owner_note`), the assistant's inference (`assistant_proposal`) or text
imported from the assistant's built-in memory files (`imported`). Nothing is deleted:
supersede, expire or forget.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from typing import Callable, Iterable

SCHEMA = "quoth-ledger/1"
KINDS = ("identity", "preference", "consent", "correction", "environment", "person", "taste",
         "project_state", "episode", "pointer")
ORIGINS = ("owner_statement", "owner_note", "assistant_proposal", "imported")
# Facts stated in the owner's own verified words may be accepted without review (undoable).
AUTO_ACCEPT_KINDS = frozenset(("identity", "environment", "person", "project_state", "pointer"))
AUTO_ACCEPT_MIN_CONFIDENCE = 0.9
# Preferences, consent, corrections, taste and episodes are always reviewed by the owner.
AUTHORITY = {"owner_statement": "owner", "owner_note": "owner", "assistant_proposal": "assistant",
             "imported": "imported"}
MAX_CLAIM_CHARS = 600
SIMILAR_MIN = 0.35
_MIGRATION_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
_WORD = re.compile(r"\w+", re.UNICODE)

Verifier = Callable[[str, str], bool]


class LedgerRejected(ValueError):
    """A proposal or review action that the ledger refuses; `code` is stable."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _fmt(moment: datetime) -> str:
    # Same shape as SQLite's strftime('%Y-%m-%dT%H:%M:%fZ'): millisecond precision, UTC.
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _now() -> str:
    return _fmt(datetime.now(UTC))


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def normalize(text: str) -> str:
    folded = unicodedata.normalize("NFC", text).casefold()
    return re.sub(r"[\s.!?;:,]+$", "", re.sub(r"\s+", " ", folded)).strip()


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(unicodedata.normalize("NFC", text).casefold()))


def similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


# ---------------------------------------------------------------- connection

def _migrations() -> list[tuple[int, str, str]]:
    out = []
    for entry in files("quoth.ledger.migrations").iterdir():
        m = _MIGRATION_RE.match(entry.name)
        if m:
            out.append((int(m.group(1)), entry.name, entry.read_text("utf-8")))
    out.sort()
    if [v for v, _, _ in out] != list(range(1, len(out) + 1)):
        raise RuntimeError("ledger migration numbering must be contiguous from 0001")
    return out


def open_ledger(path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) the mutable ledger and apply pending ledger migrations."""
    p = Path(path)
    if str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("CREATE TABLE IF NOT EXISTS ledger_migrations (version INTEGER PRIMARY KEY,"
                 " name TEXT NOT NULL, applied_at TEXT NOT NULL"
                 " DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))")
    applied = {r[0]: r[1] for r in conn.execute("SELECT version, name FROM ledger_migrations")}
    if set(applied) - {v for v, _, _ in _migrations()}:
        conn.close()
        raise RuntimeError("ledger is newer than this code")
    # Foreign keys stay off while migrations run: a table rebuild drops and renames a table
    # that others reference (SQLite's documented procedure). Each migration checks them
    # before it commits.
    conn.execute("PRAGMA foreign_keys = OFF")
    for version, name, sql in _migrations():
        if version in applied:
            if applied[version] != name:
                conn.close()
                raise RuntimeError(f"ledger migration {version} name mismatch")
            continue
        with _tx(conn):
            for statement in _statements(sql):
                conn.execute(statement)
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError(f"ledger migration {version} breaks a foreign key")
            conn.execute("INSERT INTO ledger_migrations(version, name) VALUES (?, ?)", (version, name))
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _statements(sql: str) -> Iterable[str]:
    buf = ""
    for line in sql.splitlines(keepends=True):
        if line.lstrip().startswith("--") and not buf.strip():
            continue
        buf += line
        if sqlite3.complete_statement(buf):
            yield buf
            buf = ""
    if buf.strip():
        raise RuntimeError("incomplete ledger migration statement")


@contextmanager
def _tx(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _event(conn, claim_id, action, actor, now, **detail):
    conn.execute("INSERT INTO ledger_events(claim_id, action, actor, at, detail) VALUES (?,?,?,?,?)",
                 (claim_id, action, actor, now,
                  json.dumps(detail, ensure_ascii=False, sort_keys=True) if detail else None))


def get(conn: sqlite3.Connection, claim_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
    return _view(row) if row else None


def _view(row: sqlite3.Row) -> dict:
    d = {k: row[k] for k in row.keys()}
    d["authority"] = AUTHORITY[d["origin"]]
    d["current"] = (d["review_status"] in ("pending", "accepted")
                    and d["valid_to"] is None and d["superseded_by"] is None)
    return d


_CURRENT = ("review_status IN ('pending', 'accepted') AND valid_to IS NULL"
            " AND superseded_by IS NULL")


def _current(conn) -> list[sqlite3.Row]:
    return conn.execute(f"SELECT * FROM claims WHERE {_CURRENT} ORDER BY id").fetchall()


# ---------------------------------------------------------------- proposals

def propose(conn: sqlite3.Connection, *, text: str, kind: str, origin: str, proposer: str,
            reason: str | None = None, evidence_quote: str | None = None,
            evidence_locator: str | None = None, confidence: float | None = None,
            subject_key: str | None = None, supersedes: int | None = None,
            scope: str = "general", verifier: Verifier | None = None,
            now: str | None = None) -> dict:
    """Record a pending claim after write-time reconcile; may auto-accept (see AUTO_ACCEPT_KINDS).

    Returns {decision, claim, closest, auto_accepted}. decision is one of
    add | noop_duplicate | replace_pending | supersede_proposed.
    """
    now = now or _now()
    text = (text or "").strip()
    if not text or len(text) > MAX_CLAIM_CHARS:
        raise LedgerRejected("text_length")
    if kind not in KINDS and not (kind == "unclassified" and origin == "imported"):
        raise LedgerRejected("unknown_kind")
    if origin not in ORIGINS:
        raise LedgerRejected("unknown_origin")
    if not proposer:
        raise LedgerRejected("proposer_required")
    if confidence is not None and not 0 <= confidence <= 1:
        raise LedgerRejected("confidence_range")
    verified = 0
    if origin == "owner_statement":
        if not evidence_quote or not evidence_locator:
            raise LedgerRejected("quote_required")
        if verifier is None or not verifier(evidence_locator, evidence_quote):
            raise LedgerRejected("quote_not_verified")
        verified = 1
    elif origin == "owner_note" and not evidence_locator:
        raise LedgerRejected("locator_required")
    key = normalize(text)
    with _tx(conn):
        current = _current(conn)
        for row in current:
            if normalize(row["text"]) == key:
                _event(conn, row["id"], "re_proposed", proposer, now, origin=origin)
                return {"decision": "noop_duplicate", "claim": _view(row), "closest": [],
                        "auto_accepted": False}
        if supersedes is not None:
            target = conn.execute(f"SELECT id FROM claims WHERE id = ? AND {_CURRENT}",
                                  (supersedes,)).fetchone()
            if target is None:
                raise LedgerRejected("supersedes_not_current")
        decision = "add"
        replaced_pending = []
        if subject_key:
            for row in current:
                if row["subject_key"] != subject_key:
                    continue
                if row["review_status"] == "pending":
                    replaced_pending.append(row["id"])
                elif supersedes is None:
                    supersedes = row["id"]
                    decision = "supersede_proposed"
        if supersedes is not None:
            decision = "supersede_proposed"
        cur = conn.execute(
            "INSERT INTO claims(text, kind, origin, proposer, subject_key, reason, evidence_quote,"
            " evidence_locator, quote_verified, confidence, scope, supersedes, recorded_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (text, kind, origin, proposer, subject_key, reason, evidence_quote, evidence_locator,
             verified, confidence, scope, supersedes, now))
        claim_id = cur.lastrowid
        _event(conn, claim_id, "proposed", proposer, now, origin=origin, kind=kind, decision=decision)
        for old in replaced_pending:
            # Latest-wins collapse: an older pending proposal for the same slot expires.
            conn.execute("UPDATE claims SET review_status = 'expired', superseded_at = ?,"
                         " superseded_by = ? WHERE id = ?", (now, claim_id, old))
            _event(conn, old, "expired_latest_wins", proposer, now, by=claim_id)
            _restore_duplicates_of(conn, old, proposer, now, None)  # copies dropped in its favour return
        if replaced_pending and decision == "add":
            decision = "replace_pending"
        closest = sorted(
            ({"id": r["id"], "text": r["text"], "kind": r["kind"], "review_status": r["review_status"],
              "authority": AUTHORITY[r["origin"]], "similarity": round(similarity(text, r["text"]), 3)}
             for r in current if r["id"] not in replaced_pending),
            key=lambda c: (-c["similarity"], c["id"]))
        closest = [c for c in closest if c["similarity"] >= SIMILAR_MIN][:3]
        auto = (origin == "owner_statement" and verified and kind in AUTO_ACCEPT_KINDS
                and confidence is not None and confidence >= AUTO_ACCEPT_MIN_CONFIDENCE)
        if auto:
            _accept(conn, claim_id, "auto", now, None, auto=True)
    return {"decision": decision, "claim": get(conn, claim_id), "closest": closest,
            "auto_accepted": bool(auto)}


# ---------------------------------------------------------------- review

def _pending(conn, claim_id) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
    if row is None:
        raise LedgerRejected("not_found")
    if row["review_status"] != "pending" or row["superseded_by"] is not None:
        raise LedgerRejected("not_pending")
    return row


def _batch(batch):
    # A review decision's batch is the locator of the owner's approving message; undo reverts one.
    return {"batch": batch} if batch else {}


def _accept(conn, claim_id, actor, now, note, *, auto=False, batch=None):
    row = _pending(conn, claim_id)
    if row["kind"] == "unclassified":
        raise LedgerRejected("kind_required")
    conn.execute("UPDATE claims SET review_status = 'accepted', reviewed_at = ?, review_note = ?,"
                 " auto_accepted = ?, valid_from = coalesce(valid_from, ?) WHERE id = ?",
                 (now, note, int(auto), now, claim_id))
    if row["supersedes"] is not None:
        target = conn.execute("SELECT * FROM claims WHERE id = ?", (row["supersedes"],)).fetchone()
        if target["superseded_by"] is None and target["valid_to"] is None:
            status = "expired" if target["review_status"] == "pending" else target["review_status"]
            conn.execute("UPDATE claims SET review_status = ?, valid_to = ?, superseded_at = ?,"
                         " superseded_by = ? WHERE id = ?",
                         (status, now if status == "accepted" else None, now, claim_id, target["id"]))
            _event(conn, target["id"], "superseded", actor, now, by=claim_id)
    _event(conn, claim_id, "auto_accepted" if auto else "accepted", actor, now, **_batch(batch))


def accept(conn, claim_id: int, actor: str = "owner", note: str | None = None, now: str | None = None,
           batch: str | None = None) -> dict:
    with _tx(conn):
        _accept(conn, claim_id, actor, now or _now(), note, batch=batch)
    return get(conn, claim_id)


def reject(conn, claim_id: int, actor: str = "owner", note: str | None = None, now: str | None = None,
           batch: str | None = None, duplicate_of: int | None = None) -> dict:
    """Reject a pending claim. `duplicate_of` records that it was dropped as a copy of that claim.

    Copies earlier rejected as duplicates of this claim return to review in the same
    transaction (`restored_duplicates`): with their original dropped, nothing would keep the fact.
    Expiry (latest-wins, age) restores them the same way.
    """
    now = now or _now()
    with _tx(conn):
        _pending(conn, claim_id)
        conn.execute("UPDATE claims SET review_status = 'rejected', reviewed_at = ?, review_note = ?"
                     " WHERE id = ?", (now, note, claim_id))
        _event(conn, claim_id, "rejected", actor, now, **_batch(batch),
               **({"duplicate_of": duplicate_of} if duplicate_of is not None else {}))
        restored = _restore_duplicates_of(conn, claim_id, actor, now, batch)
    return {**get(conn, claim_id), "restored_duplicates": restored}


def _restore_duplicates_of(conn, target: int, actor, now, batch) -> list[int]:
    restored = []
    for (cid,) in conn.execute("SELECT DISTINCT claim_id FROM ledger_events WHERE action = 'rejected'"
                               " AND json_extract(detail, '$.duplicate_of') = ? ORDER BY claim_id",
                               (target,)).fetchall():
        row = conn.execute("SELECT review_status, superseded_by FROM claims WHERE id = ?", (cid,)).fetchone()
        last = conn.execute("SELECT json_extract(detail, '$.duplicate_of') FROM ledger_events"
                            " WHERE claim_id = ? AND action = 'rejected' ORDER BY id DESC LIMIT 1",
                            (cid,)).fetchone()
        if row["review_status"] != "rejected" or row["superseded_by"] is not None or last[0] != target:
            continue  # back in review already, or rejected again for another reason
        conn.execute("UPDATE claims SET review_status = 'pending', reviewed_at = NULL, review_note = NULL"
                     " WHERE id = ?", (cid,))
        _event(conn, cid, "restored", actor, now, target_dropped=target, **_batch(batch))
        restored.append(cid)
    return restored


def edit(conn, claim_id: int, *, text: str | None = None, kind: str | None = None,
         actor: str = "owner", note: str | None = None, now: str | None = None,
         batch: str | None = None) -> dict:
    """The owner rewrites a pending or accepted claim: a new accepted claim supersedes it."""
    now = now or _now()
    with _tx(conn):
        old = conn.execute(f"SELECT * FROM claims WHERE id = ? AND {_CURRENT}", (claim_id,)).fetchone()
        if old is None:
            raise LedgerRejected("not_current")
        new_text = (text or old["text"]).strip()
        new_kind = kind or old["kind"]
        if not new_text or len(new_text) > MAX_CLAIM_CHARS:
            raise LedgerRejected("text_length")
        if new_kind not in KINDS:
            raise LedgerRejected("kind_required" if new_kind == "unclassified" else "unknown_kind")
        cur = conn.execute(
            "INSERT INTO claims(text, kind, origin, proposer, subject_key, reason, evidence_quote,"
            " evidence_locator, quote_verified, confidence, scope, supersedes, recorded_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (new_text, new_kind, old["origin"], "review_edit", old["subject_key"], old["reason"],
             old["evidence_quote"], old["evidence_locator"], old["quote_verified"],
             old["confidence"], old["scope"], claim_id, now))
        _event(conn, cur.lastrowid, "edited_from", actor, now, previous=claim_id, **_batch(batch))
        _accept(conn, cur.lastrowid, actor, now, note, batch=batch)
    return get(conn, cur.lastrowid)


def forget(conn, claim_id: int, reason: str, actor: str = "owner", now: str | None = None,
           batch: str | None = None) -> dict:
    now = now or _now()
    with _tx(conn):
        row = conn.execute(f"SELECT * FROM claims WHERE id = ? AND {_CURRENT}", (claim_id,)).fetchone()
        if row is None or row["review_status"] != "accepted":
            raise LedgerRejected("not_accepted")
        conn.execute("UPDATE claims SET valid_to = ?, forget_reason = ? WHERE id = ?",
                     (now, reason or None, claim_id))
        _event(conn, claim_id, "forgotten", actor, now, **_batch(batch))
    return get(conn, claim_id)


def undo_auto_accept(conn, claim_id: int, actor: str = "owner", now: str | None = None,
                     batch: str | None = None) -> dict:
    """Return an auto-accepted claim to review and restore the claim it superseded."""
    now = now or _now()
    with _tx(conn):
        row = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
        if row is None or not row["auto_accepted"] or row["review_status"] != "accepted" \
                or row["valid_to"] is not None or row["superseded_by"] is not None:
            raise LedgerRejected("not_auto_accepted")
        conn.execute("UPDATE claims SET review_status = 'pending', reviewed_at = NULL,"
                     " auto_accepted = 0 WHERE id = ?", (claim_id,))
        _restore_superseded(conn, claim_id, actor, now, undo_of=claim_id)
        _event(conn, claim_id, "auto_accept_undone", actor, now, **_batch(batch))
    return get(conn, claim_id)


def _restore_superseded(conn, claim_id, actor, now, **detail) -> list[int]:
    """Undo what accepting `claim_id` retired: expired pending claims return to review."""
    restored = []
    for target in conn.execute("SELECT id, review_status FROM claims WHERE superseded_by = ?",
                               (claim_id,)).fetchall():
        status = "pending" if target["review_status"] == "expired" else target["review_status"]
        conn.execute("UPDATE claims SET review_status = ?, valid_to = NULL, superseded_at = NULL,"
                     " superseded_by = NULL WHERE id = ?", (status, target["id"]))
        _event(conn, target["id"], "restored", actor, now, **detail)
        restored.append(target["id"])
    return restored


def expirable(conn, max_age_days: float = 30, now: str | None = None) -> list[int]:
    """Ids of pending claims older than `max_age_days`: what `expire_pending` would expire."""
    cutoff = _fmt(_parse(now or _now()) - timedelta(days=max_age_days))
    return [r[0] for r in conn.execute(
        "SELECT id FROM claims WHERE review_status = 'pending' AND superseded_by IS NULL"
        " AND recorded_at < ? ORDER BY id", (cutoff,))]


def expire_pending(conn, max_age_days: float = 30, actor: str = "expiry", now: str | None = None) -> list[int]:
    now = now or _now()
    with _tx(conn):
        ids = expirable(conn, max_age_days, now)
        for claim_id in ids:
            conn.execute("UPDATE claims SET review_status = 'expired' WHERE id = ?", (claim_id,))
            _event(conn, claim_id, "expired_age", actor, now, max_age_days=max_age_days)
            _restore_duplicates_of(conn, claim_id, actor, now, None)
    return ids


# ---------------------------------------------------------------- presort suggestions

SUGGEST_ACTIONS = ("accept", "reject", "duplicate", "stale", "keep", "covered")
# Buckets the owner may clear in one step (`bucket_state` + `apply_suggestions`); none adds memory.
# BUCKET_CAP bounds what one approving message may decide, bucket or ids.
JUNK_BUCKETS = ("duplicate", "covered", "stale", "reject")
MAX_TOPIC_CHARS = 40
MAX_LOCATOR_CHARS = 300
BUCKET_CAP = 200


def suggest(conn, claim_id: int, *, action: str, by: str, suggested_kind: str | None = None,
            rewrite: str | None = None, duplicate_of: int | None = None, note: str | None = None,
            topic: str | None = None, evidence_locator: str | None = None,
            now: str | None = None) -> dict:
    """Record a non-binding presort suggestion for a pending claim; the latest one per claim wins.

    Nothing about the claim changes. `apply_suggestions` (with the owner's approval) acts on it.
    A re-suggestion without a topic keeps the previous one. `covered` needs the vault locator
    of the note that already says it.
    """
    now = now or _now()
    if action not in SUGGEST_ACTIONS:
        raise LedgerRejected("unknown_action")
    if not by:
        raise LedgerRejected("proposer_required")
    if suggested_kind is not None and suggested_kind not in KINDS:
        raise LedgerRejected("unknown_kind")
    rewrite = (rewrite or "").strip() or None
    if rewrite is not None and len(rewrite) > MAX_CLAIM_CHARS:
        raise LedgerRejected("text_length")
    topic = " ".join((topic or "").split()) or None
    if topic is not None and len(topic) > MAX_TOPIC_CHARS:
        raise LedgerRejected("topic_length")
    evidence_locator = (evidence_locator or "").strip() or None
    if action == "covered" and evidence_locator is None:
        raise LedgerRejected("locator_required")
    if evidence_locator is not None and len(evidence_locator) > MAX_LOCATOR_CHARS:
        raise LedgerRejected("locator_length")
    with _tx(conn):
        row = _pending(conn, claim_id)
        if action == "accept" and row["kind"] == "unclassified" and suggested_kind is None:
            raise LedgerRejected("kind_required")
        if action == "duplicate":
            target = conn.execute(f"SELECT id FROM claims WHERE id = ? AND {_CURRENT}",
                                  (duplicate_of,)).fetchone() if duplicate_of is not None else None
            if target is None or duplicate_of == claim_id:
                raise LedgerRejected("duplicate_of_not_current")
        previous = conn.execute("SELECT id, topic FROM claim_suggestions WHERE claim_id = ?"
                                " AND replaced_by IS NULL", (claim_id,)).fetchone()
        if topic is None and previous is not None:
            topic = previous["topic"]
        cur = conn.execute(
            "INSERT INTO claim_suggestions(claim_id, action, suggested_kind, rewrite, duplicate_of, note,"
            " suggested_by, suggested_at, topic, evidence_locator) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (claim_id, action, suggested_kind, rewrite, duplicate_of if action == "duplicate" else None,
             note, by, now, topic, evidence_locator if action == "covered" else None))
        if previous is not None:
            conn.execute("UPDATE claim_suggestions SET replaced_by = ? WHERE id = ?", (cur.lastrowid, previous[0]))
        _event(conn, claim_id, "suggested", by, now, suggestion=action)
    return current_suggestion(conn, claim_id)


def presort_summary(conn) -> dict:
    """Pending claims by current suggestion action ('none' = not presorted yet)."""
    rows = conn.execute(
        "SELECT coalesce(s.action, 'none'), count(*) FROM claims c LEFT JOIN claim_suggestions s"
        " ON s.claim_id = c.id AND s.replaced_by IS NULL"
        " WHERE c.review_status = 'pending' AND c.superseded_by IS NULL GROUP BY 1").fetchall()
    return {action: n for action, n in sorted(tuple(r) for r in rows)}


def current_suggestion(conn, claim_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM claim_suggestions WHERE claim_id = ? AND replaced_by IS NULL",
                       (claim_id,)).fetchone()
    return {k: row[k] for k in row.keys()} if row else None


def apply_suggestions(conn, claim_ids: Iterable[int], *, actor: str = "owner", note: str | None = None,
                      now: str | None = None, batch: str | None = None) -> list[dict]:
    """Apply each pending claim's current suggestion as the owner's decision (one transaction per claim).

    accept -> accept, or edit (new accepted claim) when the suggestion sets a kind or rewrite;
    reject / stale / duplicate / covered -> reject with the reason in the review note;
    keep -> left pending. `batch` (the approving message) lets `undo_batch` revert the lot.
    """
    results = []
    for claim_id in claim_ids:
        tip = current_suggestion(conn, claim_id)
        try:
            if tip is None:
                raise LedgerRejected("no_suggestion")
            action = tip["action"]
            why = {"reject": "suggested reject", "stale": "stale",
                   "duplicate": f"duplicate of #{tip['duplicate_of']}",
                   "covered": f"already in notes: {tip['evidence_locator']}"}.get(action)
            review_note = "; ".join(x for x in (why, note) if x) or None
            if action == "keep":
                raise LedgerRejected("suggestion_is_keep")
            if action == "duplicate" and kept_target(conn, tip["duplicate_of"]) is None:
                # Never drop a copy unless the claim it folds into stays (no mutual or rejected targets).
                raise LedgerRejected("duplicate_target_not_kept")
            if action == "accept":
                row = get(conn, claim_id)
                if tip["rewrite"] or (tip["suggested_kind"] and tip["suggested_kind"] != row["kind"]):
                    out = edit(conn, claim_id, text=tip["rewrite"], kind=tip["suggested_kind"],
                               actor=actor, note=note, now=now, batch=batch)
                else:
                    out = accept(conn, claim_id, actor=actor, note=note, now=now, batch=batch)
            else:
                out = reject(conn, claim_id, actor=actor, note=review_note, now=now, batch=batch,
                             duplicate_of=tip["duplicate_of"] if action == "duplicate" else None)
            results.append({"id": claim_id, "applied": action, "result_id": out["id"],
                            "review_status": out["review_status"], "text": _short(out["text"])}
                           | ({"restored_duplicates": out["restored_duplicates"]}
                              if out.get("restored_duplicates") else {}))
        except LedgerRejected as exc:
            results.append({"id": claim_id, "error": exc.code})
    return results


def bucket_state(conn, bucket: str) -> tuple[list[int], str]:
    """(oldest-first ids of pending claims whose current suggestion is `bucket`, stamp).

    The stamp hashes the sorted (claim id, suggestion id) pairs, so it changes whenever a
    claim enters or leaves the bucket or is re-sorted: an approval for what the owner saw cannot
    widen to more.
    """
    if bucket not in JUNK_BUCKETS:
        raise LedgerRejected("unknown_bucket")
    rows = conn.execute(
        "SELECT c.id, s.id FROM claims c JOIN claim_suggestions s ON s.claim_id = c.id AND s.replaced_by IS NULL"
        " WHERE c.review_status = 'pending' AND c.superseded_by IS NULL AND s.action = ?"
        " ORDER BY c.recorded_at, c.id", (bucket,)).fetchall()
    pairs = json.dumps(sorted([r[0], r[1]] for r in rows))
    return [r[0] for r in rows], hashlib.sha256(pairs.encode()).hexdigest()[:16]


def kept_target(conn, claim_id: int | None, depth: int = 8) -> dict | None:
    """The claim a duplicate folds into, if it stays: accepted and current (following edits),
    or pending with a current `accept` suggestion. None when nothing would be kept."""
    for _ in range(depth):
        row = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone() if claim_id else None
        if row is None:
            return None
        if row["superseded_by"] is not None:
            claim_id = row["superseded_by"]
            continue
        if row["review_status"] == "accepted" and row["valid_to"] is None:
            return _view(row)
        tip = current_suggestion(conn, claim_id) if row["review_status"] == "pending" else None
        return _view(row) | {"suggestion": tip} if tip and tip["action"] == "accept" else None
    return None


def _short(text: str, n: int = 80) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


# ---------------------------------------------------------------- undo a review batch

DECISION_EVENTS = ("accepted", "rejected", "forgotten", "auto_accept_undone")
UNDOABLE_EVENTS = ("accepted", "rejected")


def batch_decisions(conn, batch: str) -> list[tuple[int, str]]:
    """(claim_id, event) of the decisions made under `batch` since it was last undone, newest first."""
    since = conn.execute(
        "SELECT coalesce(max(id), 0) FROM ledger_events WHERE action = 'review_undone'"
        " AND json_extract(detail, '$.undo_of') = ?", (batch,)).fetchone()[0]
    return [(r[0], r[1]) for r in conn.execute(
        f"SELECT claim_id, action FROM ledger_events WHERE action IN {DECISION_EVENTS}"
        " AND id > ? AND json_extract(detail, '$.batch') = ? ORDER BY id DESC", (since, batch))]


def batch_used(conn, batch: str) -> dict:
    """How an approving message has been used: decisions recorded under it, and whether undone."""
    total = conn.execute(f"SELECT count(*) FROM ledger_events WHERE action IN {DECISION_EVENTS}"
                         " AND json_extract(detail, '$.batch') = ?", (batch,)).fetchone()[0]
    undone = conn.execute("SELECT 1 FROM ledger_events WHERE action = 'review_undone'"
                          " AND json_extract(detail, '$.undo_of') = ? LIMIT 1", (batch,)).fetchone()
    return {"decisions": total, "undone": undone is not None}


def _via(via):
    return {"via": via} if via else {}


def undo_batch(conn, batch: str, *, actor: str = "owner", via: str | None = None,
               now: str | None = None) -> dict:
    """Revert the decisions made under one approving message (`batch`) as new ledger events.

    Rejected and accepted claims return to pending (and what an accept retired comes back);
    a claim an edit created is rejected and the claim it replaced returns to review. A claim
    that changed since (forgotten, superseded) is left alone and reported. Undoing the same
    batch again refuses (`nothing_to_undo`), and so does a batch holding a forget or an
    undo_auto (`not_undoable`); nothing is ever deleted. `via` is the locator of the message
    that asked for the undo.
    """
    now = now or _now()
    results, errors = [], []
    with _tx(conn):
        decisions = batch_decisions(conn, batch) if batch else []
        if not decisions:
            raise LedgerRejected("nothing_to_undo")
        if any(event not in UNDOABLE_EVENTS for _, event in decisions):
            raise LedgerRejected("not_undoable")
        for claim_id, decision in decisions:
            row = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
            if decision == "rejected":
                if row["review_status"] != "rejected" or row["superseded_by"] is not None:
                    errors.append({"id": claim_id, "error": "changed_since"})
                    continue
                conn.execute("UPDATE claims SET review_status = 'pending', reviewed_at = NULL,"
                             " review_note = NULL WHERE id = ?", (claim_id,))
                _event(conn, claim_id, "review_undone", actor, now, undo_of=batch, was="rejected", **_via(via))
                results.append({"id": claim_id, "undone": "rejected", "review_status": "pending"})
                continue
            if row["review_status"] != "accepted" or row["valid_to"] is not None \
                    or row["superseded_by"] is not None:
                errors.append({"id": claim_id, "error": "changed_since"})
                continue
            edited = row["proposer"] == "review_edit" and row["supersedes"] is not None
            if edited:
                # The claim the owner's edit created leaves memory; the one it replaced returns.
                conn.execute("UPDATE claims SET review_status = 'rejected', reviewed_at = ?,"
                             " review_note = ? WHERE id = ?",
                             (now, "edit undone" + (f" via {via}" if via else ""), claim_id))
            else:
                conn.execute("UPDATE claims SET review_status = 'pending', reviewed_at = NULL,"
                             " review_note = NULL WHERE id = ?", (claim_id,))
            restored = _restore_superseded(conn, claim_id, actor, now, undo_of=batch)
            was = "edited" if edited else "accepted"
            _event(conn, claim_id, "review_undone", actor, now, undo_of=batch, was=was, **_via(via))
            results.append({"id": claim_id, "undone": was, "restored": restored,
                            "review_status": "rejected" if edited else "pending"})
    return {"batch": batch, "results": results, "errors": errors}


# ---------------------------------------------------------------- guided review digest

ROUND_SIZE = 10
MAX_GROUP_ITEMS = 50
OTHER_TOPIC = "Other"
# Order inside a group: what would become memory first, then what would be dropped.
ITEM_ORDER = ("accept", "keep", "none", "duplicate", "covered", "stale", "reject")
TO_READ = ("accept", "keep", "none")


def _pending_with_tips(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT c.id, c.text, c.kind, s.action, s.suggested_kind, s.rewrite, s.duplicate_of, s.note,"
        " s.topic, s.evidence_locator FROM claims c LEFT JOIN claim_suggestions s"
        " ON s.claim_id = c.id AND s.replaced_by IS NULL"
        " WHERE c.review_status = 'pending' AND c.superseded_by IS NULL ORDER BY c.recorded_at, c.id")
    return [{**{k: r[k] for k in r.keys()}, "action": r["action"] or "none",
             "topic": r["topic"] or OTHER_TOPIC} for r in rows]


def digest(conn, *, topic: str | None = None, bucket: str | None = None,
           limit: int = ROUND_SIZE) -> dict:
    """The guided-review plan: pending counts per suggestion and per topic, and one group's items.

    The group is `topic`, else `bucket` (a suggestion action or 'none'), else the first topic
    (by name, 'Other' last) that still has items to read. Items are numbered from 1 for one
    chat message; `text` is the suggested rewrite when there is one.
    """
    rows = _pending_with_tips(conn)
    by_suggestion: dict[str, int] = {}
    topics: dict[str, dict] = {}
    for r in rows:
        by_suggestion[r["action"]] = by_suggestion.get(r["action"], 0) + 1
        t = topics.setdefault(r["topic"], {"topic": r["topic"], "total": 0, "to_read": 0,
                                           "by_suggestion": {}})
        t["total"] += 1
        t["to_read"] += r["action"] in TO_READ
        t["by_suggestion"][r["action"]] = t["by_suggestion"].get(r["action"], 0) + 1
    ordered = sorted(topics.values(), key=lambda t: (t["topic"] == OTHER_TOPIC, t["topic"].casefold()))
    cleanup = sum(by_suggestion.get(b, 0) for b in JUNK_BUCKETS)
    if topic is not None:
        name = next((t["topic"] for t in ordered if t["topic"].casefold() == topic.strip().casefold()), None)
        if name is None:
            raise LedgerRejected("unknown_topic")
        group, members = {"topic": name}, [r for r in rows if r["topic"] == name]
    elif bucket is not None:
        if bucket not in ITEM_ORDER:
            raise LedgerRejected("unknown_bucket")
        group, members = {"bucket": bucket}, [r for r in rows if r["action"] == bucket]
    else:
        first = next((t for t in ordered if t["to_read"]), ordered[0] if ordered else None)
        group = {"topic": first["topic"]} if first else {}
        members = [r for r in rows if first and r["topic"] == first["topic"]]
    members.sort(key=lambda r: ITEM_ORDER.index(r["action"]))  # stable: oldest first per action
    by_id = {r["id"]: r for r in rows}
    items = []
    for n, r in enumerate(members[:max(1, min(int(limit), MAX_GROUP_ITEMS))], 1):
        item = {"n": n, "id": r["id"], "text": r["rewrite"] or r["text"], "suggestion": r["action"],
                "kind": r["suggested_kind"] or r["kind"], "topic": r["topic"]}
        if r["rewrite"]:
            item["original"] = r["text"]
        if r["note"]:
            item["note"] = r["note"]
        if r["action"] == "duplicate":
            # What stays: the kept claim's suggested rewrite or text; None when nothing would stay.
            kept = kept_target(conn, r["duplicate_of"])
            kept_text = kept and ((by_id.get(kept["id"]) or {}).get("rewrite") or kept["text"])
            item.update(duplicate_of=r["duplicate_of"], duplicate_target_kept=kept is not None,
                        duplicate_of_text=kept_text or None)
        if r["action"] == "covered":
            item["evidence_locator"] = r["evidence_locator"]
        items.append(item)
    to_read = len(rows) - cleanup
    buckets = {}  # what a bucket apply must name: count and stamp as the owner saw them
    for b in JUNK_BUCKETS:
        ids, stamp = bucket_state(conn, b)
        buckets[b] = {"count": len(ids), "stamp": stamp}
    return {"pending": len(rows), "by_suggestion": dict(sorted(by_suggestion.items())), "topics": ordered,
            "buckets": buckets,
            "plan": {"cleanup": cleanup, "to_read": to_read, "unsorted": by_suggestion.get("none", 0),
                     "round_size": ROUND_SIZE, "rounds": math.ceil(to_read / ROUND_SIZE)},
            "group": {**group, "total": len(members), "items": items}}


# ---------------------------------------------------------------- reading

def review_queue(conn, limit: int = 20, *, suggested: bool | None = None, offset: int = 0) -> list[dict]:
    """Oldest-first pending claims, each with its current presort suggestion (or None).

    `suggested=False` lists only claims nobody has presorted yet (the presort worklist);
    `suggested=True` only presorted ones (the batch the owner confirms).
    """
    where = ""
    if suggested is not None:
        where = (" AND " + ("" if suggested else "NOT ") + "EXISTS (SELECT 1 FROM claim_suggestions s"
                 " WHERE s.claim_id = claims.id AND s.replaced_by IS NULL)")
    rows = conn.execute(
        "SELECT * FROM claims WHERE review_status = 'pending' AND superseded_by IS NULL" + where +
        " ORDER BY recorded_at, id LIMIT ? OFFSET ?", (limit, offset)).fetchall()
    return [{**_view(r), "suggestion": current_suggestion(conn, r["id"])} for r in rows]


def recall(conn, query: str, k: int = 5, *, mark_used: bool = False, now: str | None = None) -> list[dict]:
    """Accepted current claims ranked by term overlap; pending claims are never served."""
    terms = _tokens(query)
    scored = []
    for row in conn.execute(f"SELECT * FROM claims WHERE review_status = 'accepted' AND {_CURRENT}"):
        overlap = len(terms & _tokens(row["text"]))
        if overlap:
            scored.append((overlap, row["id"], row))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    hits = [_view(r) for _, _, r in scored[:k]]
    if mark_used:
        mark_claims_used(conn, [h["id"] for h in hits], now=now)
    return hits


def pending_current(conn) -> list[tuple[int, str]]:
    """(id, text) of every claim waiting for review, oldest first."""
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT id, text FROM claims WHERE review_status = 'pending' AND superseded_by IS NULL"
        " ORDER BY recorded_at, id")]


def accepted_current(conn) -> list[tuple[int, str]]:
    """(id, text) of every claim that recall may serve."""
    return [(r[0], r[1]) for r in conn.execute(
        f"SELECT id, text FROM claims WHERE review_status = 'accepted' AND {_CURRENT} ORDER BY id")]


def servable(conn, claim_ids: Iterable[int]) -> list[dict]:
    """The given claims that are still accepted and current, in the given order."""
    out = []
    for claim_id in claim_ids:
        row = conn.execute(f"SELECT * FROM claims WHERE id = ? AND review_status = 'accepted' AND {_CURRENT}",
                           (claim_id,)).fetchone()
        if row is not None:
            out.append(_view(row))
    return out


def claim_events(conn, claim_id: int, *, detail: bool = False) -> list[dict]:
    """A claim's audit trail, oldest first; `detail` adds each event's decoded JSON detail."""
    out = []
    for r in conn.execute("SELECT at, action, actor, detail FROM ledger_events WHERE claim_id = ?"
                          " ORDER BY id", (claim_id,)):
        event = {"at": r[0], "action": r[1], "actor": r[2]}
        if detail:
            try:
                event["detail"] = json.loads(r[3]) if r[3] else None
            except ValueError:
                event["detail"] = None
        out.append(event)
    return out


def text_matches(claim_text: str, query: str) -> bool:
    """`find` semantics: every query word occurs in the claim, or the query is a substring."""
    return _tokens(query) <= _tokens(claim_text) or query.lower() in claim_text.lower()


def find(conn, text: str, limit: int = 20) -> list[dict]:
    """Claims of any status whose text contains every query word (case-insensitive), newest first."""
    rows = [r for r in conn.execute("SELECT * FROM claims ORDER BY id DESC") if text_matches(r["text"], text)]
    return [_view(r) for r in rows[:limit]]


def mark_claims_used(conn, claim_ids: list[int], now: str | None = None) -> None:
    if not claim_ids:
        return
    now = now or _now()
    with _tx(conn):
        conn.executemany("UPDATE claims SET use_count = use_count + 1, last_used_at = ? WHERE id = ?",
                         [(now, cid) for cid in claim_ids])


def health(conn, *, max_pending: int = 40, max_pending_age_days: float = 14,
           now: str | None = None) -> dict:
    """Queue size/age plus 7-day churn; red when the review queue is too large or too old."""
    now = now or _now()
    week = _fmt(_parse(now) - timedelta(days=7))
    pending = conn.execute("SELECT kind, recorded_at FROM claims WHERE review_status = 'pending'"
                           " AND superseded_by IS NULL").fetchall()
    oldest = min((r[1] for r in pending), default=None)
    oldest_days = round((_parse(now) - _parse(oldest)).total_seconds() / 86400, 2) if oldest else None
    events = dict(conn.execute("SELECT action, count(*) FROM ledger_events WHERE at >= ?"
                               " GROUP BY action", (week,)).fetchall())
    reasons = []
    if len(pending) > max_pending:
        reasons.append("pending_queue_large")
    if oldest_days is not None and oldest_days > max_pending_age_days:
        reasons.append("pending_queue_old")
    by_kind: dict[str, int] = {}
    for r in pending:
        by_kind[r[0]] = by_kind.get(r[0], 0) + 1
    return {
        "schema": SCHEMA, "checked_at": now, "status": "red" if reasons else "green",
        "reasons": reasons, "pending": len(pending), "pending_by_kind": dict(sorted(by_kind.items())),
        "oldest_pending_age_days": oldest_days,
        "accepted_current": conn.execute(
            f"SELECT count(*) FROM claims WHERE review_status = 'accepted' AND {_CURRENT}").fetchone()[0],
        "last_7d": {name: events.get(action, 0) for name, action in (
            ("proposed", "proposed"), ("re_proposed", "re_proposed"), ("accepted", "accepted"),
            ("auto_accepted", "auto_accepted"), ("rejected", "rejected"), ("superseded", "superseded"),
            ("expired", "expired_age"), ("forgotten", "forgotten"),
            ("auto_accept_undone", "auto_accept_undone"))},
    }


def seed(conn, rows: Iterable[dict], *, proposer: str = "seed", now: str | None = None) -> dict:
    """Import memory texts as pending `imported` claims, latest-wins by text.

    Each row: {"text", "kind"?, "reason"?}. Later rows win over earlier duplicates; texts
    already current in the ledger are skipped. Nothing is auto-accepted.
    """
    now = now or _now()
    latest: dict[str, dict] = {}
    for row in rows:
        text = str(row.get("text") or "").strip()
        if text:
            latest.pop(normalize(text), None)
            latest[normalize(text)] = row
    added = skipped = 0
    for row in latest.values():
        kind = row.get("kind") or "unclassified"
        result = propose(conn, text=str(row["text"]).strip()[:MAX_CLAIM_CHARS], kind=kind,
                         origin="imported", proposer=proposer, reason=row.get("reason"),
                         now=now)
        if result["decision"] == "noop_duplicate":
            skipped += 1
        else:
            added += 1
    return {"input_unique": len(latest), "added": added, "skipped_current_duplicates": skipped}
