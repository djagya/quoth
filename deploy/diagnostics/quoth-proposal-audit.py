#!/usr/bin/env python3
"""Aggregate-only audit of the proposals in a quoth review ledger.

Answers "where do the proposals the owner rejects come from?" with counts only. It opens
`ledger.db` read-only (`file:...?mode=ro`), changes nothing, and prints one JSON document
with NO claim text, quotes, notes, locators or ids: only labels from closed sets (origin,
kind, suggestion action, sanitised proposer names), bins and counts.

    python3 quoth-proposal-audit.py [LEDGER] [--days 30] [--now 2026-01-31T00:00:00Z]

LEDGER defaults to $QUOTH_HOME/ledger.db, else ~/.local/share/quoth/ledger.db. Standard
library only, so the copy in a release (deploy/diagnostics/) runs with any python3 >= 3.9.

One proposal is one claim the proposer wrote (claims an owner's edit created are decisions,
not proposals). Its outcome, from the ledger as it is now:

    accepted            accepted by the owner, or auto-accepted (counted apart as well)
    accepted_edited     the owner accepted it with an edit (a new claim superseded it)
    rejected            rejected by the owner
    duplicate_rejected  rejected as a duplicate of another claim
    superseded          ended without a decision: a newer proposal for its slot (latest
                        wins), or an accepted claim that superseded it
    expired             pending past the age limit (`quoth ledger expire`)
    pending             still waiting for review

`rejection_share` = (rejected + duplicate_rejected) / owner-decided (both accepted kinds and
both rejected kinds; auto-accepts excluded).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = "quoth-proposal-audit/1"
OUTCOMES = ("accepted", "accepted_edited", "rejected", "duplicate_rejected", "superseded",
            "expired", "pending")
ACCEPTED = ("accepted", "accepted_edited")
REJECTED = ("rejected", "duplicate_rejected")
# Pre-0004 ledgers used other origin names; report the current vocabulary.
OLD_ORIGINS = {"danil_statement": "owner_statement", "danil_vault": "owner_note",
               "sera_proposal": "assistant_proposal", "builtin_mirror": "imported"}
# What `apply_suggestions` writes first in a reject's review note, per suggestion action.
APPLY_NOTE_PREFIXES = ("suggested reject", "stale", "duplicate of #", "already in notes:")
MIRROR_REASON = re.compile(r"^mirrored built-in (\w{1,30}) (add|replace)$")
LABEL = re.compile(r"[a-z][a-z0-9_-]{0,30}(:[a-z][a-z0-9_-]{0,30})?")
TEXT_BINS = ((50, "<50"), (100, "50-99"), (200, "100-199"), (300, "200-299"), (450, "300-449"),
             (600, "450-599"))
TEXT_CAP = 600  # MAX_CLAIM_CHARS; the mirror route truncates longer writes to exactly this
HOUR = 3600.0
TIME_BINS = ((HOUR, "<1h"), (24 * HOUR, "1h-1d"), (72 * HOUR, "1-3d"), (168 * HOUR, "3-7d"),
             (336 * HOUR, "7-14d"), (720 * HOUR, "14-30d"))
BATCH_BINS = ((1, "1"), (9, "2-9"), (49, "10-49"))
NEAR_DEFAULT = 0.8
# Coarse shape flags, counted per outcome (the text itself is never printed). Heuristics with
# English cue words, since the assistant's built-in memory notes are mostly English.
SHAPES = {
    "url": re.compile(r"https?://", re.I),
    "path": re.compile(r"(^|[\s(`'\"])(~|\.{0,2})/[\w.-]+/"),
    "date_or_time": re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}:\d{2}\b"),
    "code_or_command": re.compile(r"`|\$\(|==|\{\}|--[a-z]"),
    "multi_line": re.compile(r"\n"),
    "says_user_or_owner": re.compile(r"\b(user|owner)('s)?\b", re.I),
    "first_person": re.compile(r"\b(I|I'm|I've|I'll|my|me)\b"),
    "instruction": re.compile(r"^\s*(always|never|remember|note|todo|use|don't|do not|must|should)\b",
                              re.I),
}
SENTENCE_END = re.compile(r"[.!?](\s|$)")
_WORD = re.compile(r"\w+", re.UNICODE)


# ---------------------------------------------------------------- helpers

def normalize(text: str) -> str:
    """The ledger's own duplicate key (quoth.ledger.core.normalize)."""
    folded = unicodedata.normalize("NFC", text).casefold()
    return re.sub(r"[\s.!?;:,]+$", "", re.sub(r"\s+", " ", folded)).strip()


def tokens(text: str) -> frozenset[str]:
    return frozenset(_WORD.findall(unicodedata.normalize("NFC", text).casefold()))


def parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        moment = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def label(value: str | None) -> str:
    """A proposer or suggester name, kept only when it is a plain label (never free text)."""
    if value is None:
        return "none"
    return value if LABEL.fullmatch(value) else "other"


def text_bin(n: int) -> str:
    if n >= TEXT_CAP:
        return f"{TEXT_CAP} (cap)"
    return next(name for limit, name in TEXT_BINS if n < limit)


def time_bin(seconds: float) -> str:
    return next((name for limit, name in TIME_BINS if seconds < limit), ">30d")


def batch_bin(size: int) -> str:
    return next((name for limit, name in BATCH_BINS if size <= limit), "50+")


def share(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def load_json(detail: str | None) -> dict:
    try:
        value = json.loads(detail) if detail else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------- reading

def connect(path: Path) -> tuple[sqlite3.Connection, str]:
    if not path.is_file():
        raise SystemExit(f"no ledger at {path}")
    uri = path.resolve().as_uri()
    try:
        conn = sqlite3.connect(uri + "?mode=ro", uri=True)
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        mode = "ro"
    except sqlite3.OperationalError:
        # A WAL ledger in a directory this user cannot write: read the file as it is.
        conn = sqlite3.connect(uri + "?mode=ro&immutable=1", uri=True)
        mode = "ro_immutable"
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn, mode


def has_table(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (name,)).fetchone() is not None


def load(conn) -> dict:
    claims = {r["id"]: dict(r) for r in conn.execute(
        "SELECT id, text, kind, origin, proposer, subject_key, reason, quote_verified, review_status,"
        " auto_accepted, supersedes, recorded_at, reviewed_at, review_note, superseded_at,"
        " superseded_by, valid_to FROM claims ORDER BY id")}
    events = [dict(r) | {"detail": load_json(r["detail"])} for r in conn.execute(
        "SELECT id, claim_id, action, actor, at, detail FROM ledger_events ORDER BY id")]
    suggestions = {}
    if has_table(conn, "claim_suggestions"):
        for r in conn.execute("SELECT claim_id, action, suggested_kind, suggested_by FROM"
                              " claim_suggestions WHERE replaced_by IS NULL ORDER BY id"):
            suggestions[r["claim_id"]] = dict(r)
    migrations = []
    if has_table(conn, "ledger_migrations"):
        migrations = [r[0] for r in conn.execute("SELECT version FROM ledger_migrations ORDER BY version")]
    return {"claims": claims, "events": events, "suggestions": suggestions, "migrations": migrations}


# ---------------------------------------------------------------- per-proposal facts

def facts(data: dict) -> list[dict]:
    claims, events, suggestions = data["claims"], data["events"], data["suggestions"]
    edit_of = {}  # claim an edit created -> the claim it replaced
    last_reject: dict[int, dict] = {}
    batch_of: dict[int, str] = {}  # claim -> batch of its latest decision
    for e in events:
        cid, detail = e["claim_id"], e["detail"]
        if e["action"] == "edited_from" and isinstance(detail.get("previous"), int):
            edit_of[cid] = detail["previous"]
        if e["action"] == "rejected":
            last_reject[cid] = detail
        if e["action"] in ("accepted", "rejected", "edited_from") and isinstance(detail.get("batch"), str):
            if e["action"] == "accepted" and cid in edit_of:
                continue  # the edit's own accept: its decision is counted on the edited claim
            target = edit_of.get(cid, cid) if e["action"] == "edited_from" else cid
            batch_of[target] = detail["batch"]
    edited_by = {prev: new for new, prev in edit_of.items()}
    batch_sizes = Counter(batch_of.values())
    out = []
    for cid, c in claims.items():
        if cid in edit_of or c["proposer"] == "review_edit":
            continue  # an owner's edit, not a proposal
        status = c["review_status"]
        auto = bool(c["auto_accepted"])
        edit_claim = claims.get(c["superseded_by"]) if c["superseded_by"] is not None else None
        if status == "accepted":
            outcome = "accepted"  # an accepted claim edited later stays an accept
        elif c["superseded_by"] is not None and edit_of.get(c["superseded_by"]) == cid:
            outcome = "accepted_edited"
        elif status == "rejected":
            outcome = "duplicate_rejected" if "duplicate_of" in last_reject.get(cid, {}) else "rejected"
        elif status == "expired" or c["superseded_by"] is not None:
            outcome = "superseded" if c["superseded_by"] is not None else "expired"
        else:
            outcome = "pending"
        tip = suggestions.get(cid)
        if outcome == "accepted_edited" and edit_claim is not None:
            kind_at_decision = edit_claim["kind"]
        elif tip is not None and tip["suggested_kind"]:
            kind_at_decision = tip["suggested_kind"]
        else:
            kind_at_decision = c["kind"]
        decided_at = None
        if outcome == "accepted_edited":
            decided_at = parse_ts(c["superseded_at"])
        elif outcome in ("accepted", "rejected", "duplicate_rejected"):
            decided_at = parse_ts(c["reviewed_at"])
        recorded = parse_ts(c["recorded_at"])
        mirror = MIRROR_REASON.match(c["reason"] or "")
        reject_path = None
        if outcome in REJECTED:
            note = c["review_note"] or ""
            if "family_of" in last_reject.get(cid, {}):
                reject_path = "family_of_dropped_claim"
            elif note.startswith(APPLY_NOTE_PREFIXES):
                reject_path = "applied_suggestion"
            elif cid in batch_of:
                reject_path = "direct"
            else:
                reject_path = "no_batch"  # CLI or a pre-batch ledger
        owner_decided = outcome in ACCEPTED + REJECTED and not auto
        batch = batch_of.get(cid)
        out.append({
            "id": cid,  # internal only: never printed
            "text": c["text"],  # internal only: never printed
            "norm": normalize(c["text"]),
            "tokens": tokens(c["text"]),
            "subject_key": c["subject_key"],
            "recorded": recorded,
            "decided": decided_at,
            "outcome": outcome,
            "auto": auto,
            "owner_decided": owner_decided,
            "dims": {
                "origin": OLD_ORIGINS.get(c["origin"], c["origin"]),
                "proposer": label(c["proposer"]),
                "kind_at_proposal": c["kind"],
                "kind_at_decision": kind_at_decision,
                "quote_verified": str(c["quote_verified"]),
                "suggestion": tip["action"] if tip else "none",
                "suggested_by": label(tip["suggested_by"]) if tip else "none",
                "mirror_action": f"{mirror.group(1)}:{mirror.group(2)}" if mirror else "not_mirrored",
                "has_subject_key": str(bool(c["subject_key"])).lower(),
                "text_length": text_bin(len(c["text"])),
            },
            "decided_dims": {
                "time_to_decision": (time_bin((decided_at - recorded).total_seconds())
                                     if decided_at and recorded else "unknown"),
                "decision_batch_size": batch_bin(batch_sizes[batch]) if batch else "no_batch",
            } if owner_decided else {},
            "reject_path": reject_path,
            "shapes": [name for name, rx in SHAPES.items() if rx.search(c["text"])]
            + (["many_sentences"] if len(SENTENCE_END.findall(c["text"])) >= 3 else []),
        })
    out.sort(key=lambda f: (f["recorded"] or datetime.min.replace(tzinfo=timezone.utc), f["id"]))
    return out


# ---------------------------------------------------------------- aggregation

def _counts(rows: list[dict]) -> dict:
    c = Counter(r["outcome"] for r in rows)
    decided = sum(1 for r in rows if r["owner_decided"])
    rejected = sum(c[o] for o in REJECTED)
    return {"total": len(rows), **{o: c[o] for o in OUTCOMES},
            "auto_accepted": sum(1 for r in rows if r["auto"]),
            "owner_decided": decided, "rejection_share": share(rejected, decided)}


def _breakdown(rows: list[dict], key: str, field: str = "dims") -> dict:
    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        if key in r[field]:
            groups[r[field][key]].append(r)
    return {value: _counts(members) for value, members in sorted(groups.items())}


def presort(rows: list[dict]) -> dict:
    """How often the presort suggestion matched the owner's final decision.

    Predicted class: `accept` -> keep; reject, stale, duplicate, covered -> drop; `keep` makes
    no prediction. Actual class: accepted or accepted_edited -> keep; either rejection -> drop.
    Only owner-decided proposals that carried a suggestion count. A bulk `apply` makes the
    decision equal the suggestion by construction, so read precision next to `reject_path`.
    """
    decided = [r for r in rows if r["owner_decided"]]
    with_tip = [r for r in decided if r["dims"]["suggestion"] != "none"]
    per_action = {}
    for action in sorted({r["dims"]["suggestion"] for r in with_tip}):
        members = [r for r in with_tip if r["dims"]["suggestion"] == action]
        kept = sum(1 for r in members if r["outcome"] in ACCEPTED)
        entry = {"decided": len(members), "owner_kept": kept, "owner_dropped": len(members) - kept,
                 "owner_edited": sum(1 for r in members if r["outcome"] == "accepted_edited")}
        if action != "keep":
            matched = kept if action == "accept" else len(members) - kept
            entry |= {"matched": matched, "precision": share(matched, len(members))}
        per_action[action] = entry
    predicted = [r for r in with_tip if r["dims"]["suggestion"] != "keep"]

    def recall(actual_kept: bool) -> dict:
        actual = [r for r in predicted if (r["outcome"] in ACCEPTED) == actual_kept]
        hit = sum(1 for r in actual if (r["dims"]["suggestion"] == "accept") == actual_kept)
        return {"owner_decided": len(actual), "suggested_same": hit, "recall": share(hit, len(actual))}

    matched = sum(1 for r in predicted
                  if (r["dims"]["suggestion"] == "accept") == (r["outcome"] in ACCEPTED))
    return {"owner_decided": len(decided), "with_suggestion": len(with_tip),
            "suggestion_coverage": share(len(with_tip), len(decided)),
            "agreement": share(matched, len(predicted)), "per_action": per_action,
            "recall": {"keep": recall(True), "drop": recall(False)},
            "pending_by_suggestion": dict(sorted(Counter(
                r["dims"]["suggestion"] for r in rows if r["outcome"] == "pending").items()))}


class NearIndex:
    """Token sets by size: a Jaccard of at least t needs t <= |a| / |b| <= 1 / t."""

    def __init__(self, threshold: float):
        self.t = threshold
        self.by_size: dict[int, list] = defaultdict(list)

    def add(self, toks: frozenset, payload=None) -> None:
        if toks:
            self.by_size[len(toks)].append((toks, payload))

    def matches(self, toks: frozenset):
        if not toks:
            return
        n = len(toks)
        for size in range(math.ceil(n * self.t), math.floor(n / self.t) + 1):
            for other, payload in self.by_size.get(size, ()):
                if len(toks & other) >= self.t * len(toks | other):
                    yield payload


def _earliest(seen: dict, key: str, decided: datetime | None) -> None:
    if key not in seen:
        seen[key] = decided
    elif seen[key] is not None and (decided is None or decided < seen[key]):
        seen[key] = decided


def repeats(all_rows: list[dict], in_window, threshold: float) -> dict:
    """Re-proposals after a rejection, and rejected proposals repeating an earlier rejection.

    `all_rows` is every proposal oldest first (earlier matches count from any time); only the
    rows `in_window` accepts are counted.
    """
    rejected_norm: dict[str, datetime | None] = {}  # normalized text -> earliest rejection
    rejected_subject: dict[str, datetime | None] = {}
    rejected_seen = NearIndex(threshold)  # payload: when that rejection was decided
    any_seen = NearIndex(threshold)
    text_after = Counter()
    subject_after = Counter()
    near_after = Counter()
    repeat_exact = repeat_near = 0
    near_any = Counter()
    multiplicity: Counter = Counter()
    for r in all_rows:
        rec = r["recorded"]
        counted = in_window(r)

        def before(decided):
            return decided is None or rec is None or decided <= rec

        exact = r["norm"] in rejected_norm and before(rejected_norm[r["norm"]])
        subject = bool(r["subject_key"]) and r["subject_key"] in rejected_subject \
            and before(rejected_subject[r["subject_key"]])
        near = not exact and any(before(d) for d in rejected_seen.matches(r["tokens"]))
        if counted:
            if exact:
                text_after[r["outcome"]] += 1
            if subject:
                subject_after[r["outcome"]] += 1
            if near:
                near_after[r["outcome"]] += 1
            if r["outcome"] in REJECTED:
                repeat_exact += exact
                repeat_near += near
            if any(True for _ in any_seen.matches(r["tokens"])):
                near_any[r["outcome"]] += 1
        any_seen.add(r["tokens"])
        if r["outcome"] in REJECTED:
            if counted:
                multiplicity[r["norm"]] += 1
            _earliest(rejected_norm, r["norm"], r["decided"])
            if r["subject_key"]:
                _earliest(rejected_subject, r["subject_key"], r["decided"])
            rejected_seen.add(r["tokens"], r["decided"])
    rejected_total = sum(1 for r in all_rows if in_window(r) and r["outcome"] in REJECTED)
    copies = Counter(n for n in multiplicity.values())
    return {
        "same_text_after_rejection": dict(sorted(text_after.items())) | {"total": sum(text_after.values())},
        "same_subject_key_after_rejection": dict(sorted(subject_after.items()))
        | {"total": sum(subject_after.values())},
        "near_text_after_rejection": dict(sorted(near_after.items())) | {"total": sum(near_after.values())},
        "repeat_junk": {
            "rejected": rejected_total,
            "same_text_as_earlier_rejected": repeat_exact,
            "near_text_as_earlier_rejected": repeat_near,
            "share_exact": share(repeat_exact, rejected_total),
            "share_exact_or_near": share(repeat_exact + repeat_near, rejected_total),
            "distinct_rejected_texts": len(multiplicity),
            "texts_by_copies": {str(k): v for k, v in sorted(copies.items())},
        },
        "near_repeat_of_any_earlier_proposal": dict(sorted(near_any.items()))
        | {"total": sum(near_any.values())},
        "near_threshold": threshold,
    }


def shape_counts(rows: list[dict]) -> dict:
    """Outcomes per shape flag (a proposal may carry several); `none` = no flag."""
    out = {}
    for name in (*SHAPES, "many_sentences", "none"):
        members = [r for r in rows if name in r["shapes"] or (name == "none" and not r["shapes"])]
        if members:
            out[name] = _counts(members)
    return out


def section(all_rows: list[dict], in_window, threshold: float) -> dict:
    rows = [r for r in all_rows if in_window(r)]
    decided = [r for r in rows if r["owner_decided"]]
    rejected = [r for r in rows if r["outcome"] in REJECTED]
    return {
        **_counts(rows),
        "by": {key: _breakdown(rows, key) for key in (
            "origin", "proposer", "kind_at_proposal", "kind_at_decision", "quote_verified",
            "suggestion", "suggested_by", "mirror_action", "has_subject_key", "text_length")},
        "origin_x_suggestion": {o: _breakdown([r for r in rows if r["dims"]["origin"] == o], "suggestion")
                                for o in sorted({r["dims"]["origin"] for r in rows})},
        "time_to_decision": _breakdown(decided, "time_to_decision", "decided_dims"),
        "decision_batch_size": _breakdown(decided, "decision_batch_size", "decided_dims"),
        "reject_path": dict(sorted(Counter(r["reject_path"] for r in rejected).items())),
        "text_shape": shape_counts(rows),
        "presort": presort(rows),
        "reproposals": repeats(all_rows, in_window, threshold),
    }


def events_summary(events: list[dict], since: datetime) -> dict:
    total, recent = Counter(), Counter()
    for e in events:
        total[e["action"]] += 1
        at = parse_ts(e["at"])
        if at and at >= since:
            recent[e["action"]] += 1
    reproposed_claims = {e["claim_id"] for e in events if e["action"] == "re_proposed"}
    return {"all_time": dict(sorted(total.items())), "window": dict(sorted(recent.items())),
            "claims_re_proposed_as_noop_duplicate": len(reproposed_claims)}


def audit(path: Path, *, days: float = 30, now: datetime | None = None,
          near_threshold: float = NEAR_DEFAULT) -> dict:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    conn, mode = connect(path)
    try:
        data = load(conn)
    finally:
        conn.close()
    rows = facts(data)

    def everything(_r):
        return True

    def recent(r):
        return r["recorded"] is not None and r["recorded"] >= since

    daily: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        if recent(r):
            daily[r["recorded"].date().isoformat()][r["dims"]["origin"]] += 1
    window = f"last_{days:g}d"
    return {
        "schema": SCHEMA,
        "now": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "window_days": days,
        "ledger": {"opened": mode, "migrations": data["migrations"],
                   "claims": len(data["claims"]), "proposals": len(rows),
                   "edit_claims": len(data["claims"]) - len(rows)},
        "all_time": section(rows, everything, near_threshold),
        window: section(rows, recent, near_threshold),
        "events": events_summary(data["events"], since),
        f"proposals_per_day_{window}": {d: dict(sorted(c.items())) for d, c in sorted(daily.items())},
    }


def default_ledger() -> Path:
    home = os.environ.get("QUOTH_HOME")
    if home:
        return Path(home) / "ledger.db"
    data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data) / "quoth" / "ledger.db"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    p.add_argument("ledger", nargs="?", type=Path, default=None,
                   help="ledger.db (default $QUOTH_HOME/ledger.db)")
    p.add_argument("--days", type=float, default=30, help="window for the recent section (default 30)")
    p.add_argument("--now", help="ISO time the window ends at (default: now, UTC)")
    p.add_argument("--near", type=float, default=NEAR_DEFAULT,
                   help="word-overlap (Jaccard) threshold for near repeats (default 0.8)")
    a = p.parse_args(argv)
    now = parse_ts(a.now) if a.now else None
    if a.now and now is None:
        p.error("--now must be an ISO time")
    if not 0 < a.near <= 1 or a.days <= 0:
        p.error("--near must be in (0, 1] and --days positive")
    report = audit(a.ledger or default_ledger(), days=a.days, now=now, near_threshold=a.near)
    json.dump(report, sys.stdout, indent=2, sort_keys=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
