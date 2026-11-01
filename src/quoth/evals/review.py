"""`quoth eval review QUEUE --out SET.jsonl`: the owner turns captured misses into eval rows.

The queue is the sidecar's `--capture-misses` directory (or its `misses.jsonl`). Each pending
candidate is shown with its signal, the question, what the turn injected and the paths the
assistant's own searches returned, numbered. The owner picks the gold (numbers, typed paths
or both), marks the question as an abstain row with a reason, edits it, rejects it, skips
it or quits. An accepted candidate is appended to a private v2 set (`origin: real_user`,
`provenance.author: owner`) and removed from the queue; a rejected one is removed, so its
text is gone. Skipped candidates stay. Plain `input()`: scriptable through stdin.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import re

from .. import miss_capture as mc
from .format import ABSTAIN_REASONS, KINDS, LANG_RE, EvalSetError, load_set, row_errors, write_set

ACTIONS = "gold: numbers or vault paths, comma-separated | a abstain | e edit | r reject | s skip | q quit"
_CYRILLIC = re.compile(r"[Ѐ-ӿ]")


class ReviewError(ValueError):
    pass


def _paths_of(candidate: dict) -> list[tuple[str, str]]:
    """(path, where) numbered for the reviewer: what the searches returned, then what the turn injected."""
    out: dict[str, list[str]] = {}
    for search in candidate.get("searches") or []:
        for p in search.get("paths") or []:
            out.setdefault(p, [])
            if "returned" not in out[p]:
                out[p].append("returned")
    for p in candidate.get("injected_paths") or []:
        out.setdefault(p, []).append("injected")
    return [(p, "+".join(w)) for p, w in out.items()]


def _show(candidate: dict, n: int, total: int, numbered: list, say) -> None:
    seen = int(candidate.get("seen", 1))
    say("")
    say(f"--- candidate {n}/{total}  [{candidate.get('signal')}]  asked {candidate.get('ts')}"
        + (f"  (asked {seen}x)" if seen > 1 else ""))
    say(f"vault: {candidate.get('vault_status')}, pool {candidate.get('pool_n', 0)},"
        f" passed {candidate.get('passed_n', 0)}; claims injected {len(candidate.get('claims_injected') or [])}"
        + "".join(f"; {k} injected {v}" for k, v in sorted((candidate.get("lanes_injected") or {}).items()) if v))
    say(f"question: {candidate['question']}")
    say("injected: " + (", ".join(candidate.get("injected") or []) or "(none)"))
    for search in candidate.get("searches") or []:
        say(f"assistant searched: {search.get('query')!r}" + (f" after {search['after_s']} s"
                                                              if search.get("after_s") is not None else ""))
    if numbered:
        say("paths:")
        for i, (p, where) in enumerate(numbered, 1):
            say(f"  {i}. {p}  ({where})")
    else:
        say("paths: (none returned)")


def _valid_path(p: str) -> bool:
    pp = PurePosixPath(p)
    return bool(p) and not pp.is_absolute() and ".." not in pp.parts and "\\" not in p


def _gold(answer: str, numbered: list, known: set | None) -> tuple[list[str] | None, str | None]:
    """Gold paths from an answer, or (None, why not)."""
    if re.fullmatch(r"[\d,\s]+", answer):
        tokens = [t for t in re.split(r"[,\s]+", answer) if t]
    else:
        tokens = [t.strip() for t in answer.split(",") if t.strip()]
    paths = []
    for t in tokens:
        if t.isdigit():
            i = int(t)
            if not 1 <= i <= len(numbered):
                return None, f"no path numbered {i}"
            p = numbered[i - 1][0]
        else:
            p = t
            if not _valid_path(p):
                return None, f"not a vault-relative path: {p}"
        if known is not None and p not in known:
            return None, f"not in the snapshot: {p}"
        if p not in paths:
            paths.append(p)
    return (paths, None) if paths else (None, "no gold given")


def _ask(prompt: str, ask) -> str:
    return ask(prompt).strip()


def _choose(prompt: str, default: str, ok, ask, say) -> str:
    while True:
        got = _ask(f"{prompt} [{default}]: ", ask) or default
        why = ok(got)
        if why is None:
            return got
        say(why)


def _kind_ok(abstain: bool):
    def check(k: str):
        if k not in KINDS:
            return "kind must be one of: " + ", ".join(KINDS)
        if k == "unanswerable" and not abstain:
            return "unanswerable rows abstain: pick another kind or use 'a'"
        if k == "followup":
            return "followup rows need the preceding turns, which are not captured"
        return None
    return check


def _lang_ok(lang: str):
    return None if LANG_RE.match(lang) else "lang must be a language code (en, ru, pt-BR)"


def _reason(ask, say) -> str | None:
    listing = ", ".join(f"{i} {r}" for i, r in enumerate(ABSTAIN_REASONS, 1))
    while True:
        got = _ask(f"abstain reason ({listing}; blank cancels): ", ask)
        if not got:
            return None
        if got.isdigit() and 1 <= int(got) <= len(ABSTAIN_REASONS):
            return ABSTAIN_REASONS[int(got) - 1]
        if got in ABSTAIN_REASONS:
            return got
        say("unknown reason")


def build_row(candidate: dict, *, question: str, edited: bool, kind: str, lang: str,
              gold: list[str] | None = None, groups: bool = False, abstain_reason: str | None = None,
              reviewed_at: str | None = None) -> dict:
    """The v2 row an accepted candidate becomes."""
    signal = candidate.get("signal") or "unknown"
    sources = [{"store": "vault", "match": {"path": p}} for p in gold or []]
    expect = {"abstain": not sources, "sources": sources}
    if groups and len(sources) > 1:
        expect["groups"] = [{"name": f"g{i}", "any_of": [s]} for i, s in enumerate(sources, 1)]
    prov = {"author": "owner", "review": "edited" if edited else "accepted",
            "reviewed_at": reviewed_at or datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
            "mined_from": f"miss-capture:{signal}"}
    if candidate.get("ts"):
        prov["created_at"] = candidate["ts"]
    row = {"id": f"miss-{candidate['id']}", "question": question, "lang": lang, "kind": kind,
           "origin": "real_user", "expect": expect, "tags": ["mined:miss-capture", f"signal:{signal}"],
           "provenance": prov}
    if candidate.get("ts"):
        row["asked_at"] = candidate["ts"]
    if abstain_reason:
        row["abstain_reason"] = abstain_reason
    meta = {k: candidate[k] for k in ("snapshot_id",) if candidate.get(k)}
    if candidate.get("v"):
        meta["quoth_version"] = candidate["v"]
    if meta:
        row["meta"] = meta
    return row


def _decide(candidate: dict, numbered: list, known: set | None, ask, say):
    """('accept', row) | ('reject', None) | ('skip', None) | ('quit', None)."""
    question, edited = candidate["question"], False
    while True:
        answer = _ask("> ", ask)
        low = answer.lower()
        if low == "q":
            return "quit", None
        if low == "s":
            return "skip", None
        if low == "r":
            return "reject", None
        if low == "e":
            new = _ask("question (blank keeps it): ", ask)
            if new:
                if len(new) > 2048:
                    say("a question is at most 2048 characters")
                else:
                    question, edited = new, new != candidate["question"]
            say(f"question: {question}")
            continue
        if low == "":
            say(ACTIONS)
            continue
        lang_default = "ru" if _CYRILLIC.search(question) else "en"
        if low == "a":
            reason = _reason(ask, say)
            if reason is None:
                continue
            kind = _choose("kind", "unanswerable", _kind_ok(True), ask, say)
            lang = _choose("lang", lang_default, _lang_ok, ask, say)
            return "accept", build_row(candidate, question=question, edited=edited, kind=kind, lang=lang,
                                       abstain_reason=reason)
        gold, why = _gold(answer, numbered, known)
        if gold is None:
            say(why)
            say(ACTIONS)
            continue
        groups = False
        if len(gold) > 1:
            groups = _ask("does the answer need all of them (groups)? [y/N]: ", ask).lower() in ("y", "yes")
        kind = _choose("kind", "fact", _kind_ok(False), ask, say)
        lang = _choose("lang", lang_default, _lang_ok, ask, say)
        return "accept", build_row(candidate, question=question, edited=edited, kind=kind, lang=lang,
                                   gold=gold, groups=groups)


def _snapshot_paths(db) -> set[str]:
    from contextlib import closing
    from ..db import connect_read_only
    from ..refresh import db_inventory
    with closing(connect_read_only(db)) as conn:
        return set(db_inventory(conn))


def _remove(directory: Path, path: Path, cid: str) -> None:
    with mc.locked(directory):
        entries = mc.read_queue(path)
        mc.write_queue(path, [e for e in entries if e.get("id") != cid], sync=True)


def _load_out(out: Path) -> tuple[list[dict], str | None]:
    """Rows and fixture of an existing set (None fixture when new); refuses a public set."""
    if not out.exists():
        return [], None
    evalset = load_set(out)
    if evalset.manifest.get("privacy") != "private":
        raise ReviewError("the output set must be private: real questions never go in a public set")
    return list(evalset.rows), evalset.manifest.get("fixture")


def review(queue, out, *, db=None, fixture: str = "private", ask=input, say=print) -> dict:
    """The review loop; returns counts (no text)."""
    directory, path = mc.queue_path(queue)
    if not directory.is_dir():
        raise ReviewError("queue directory not found")
    mc.private_dir(directory)
    out = Path(out)
    rows, set_fixture = _load_out(out)
    fixture = set_fixture or fixture
    known = _snapshot_paths(db) if db else None
    with mc.locked(directory):
        pending = mc.read_queue(path)
    summary = {"pending": len(pending), "accepted": 0, "rejected": 0, "skipped": 0, "already_in_set": 0}
    if not pending:
        say("no candidates to review")
    say(ACTIONS)
    for n, candidate in enumerate(pending, 1):
        numbered = _paths_of(candidate)
        _show(candidate, n, len(pending), numbered, say)
        rid = f"miss-{candidate['id']}"
        if any(r.get("id") == rid for r in rows):
            say(f"already in the set as {rid}: removed from the queue")
            _remove(directory, path, candidate["id"])
            summary["already_in_set"] += 1
            continue
        key = mc.question_key(candidate["question"])
        same = [r["id"] for r in rows if mc.question_key(r.get("question", "")) == key]
        if same:
            say(f"note: the set already has this question ({', '.join(same[:3])})")
        try:
            action, row = _decide(candidate, numbered, known, ask, say)
        except EOFError:
            action, row = "quit", None
        if action == "quit":
            break
        if action == "skip":
            summary["skipped"] += 1
            continue
        if action == "reject":
            _remove(directory, path, candidate["id"])
            summary["rejected"] += 1
            say("rejected: removed from the queue")
            continue
        errors = row_errors(row)
        if errors:
            say("row not valid, kept in the queue: " + "; ".join(errors))
            summary["skipped"] += 1
            continue
        try:
            write_set(rows + [row], out, privacy="private", fixture=fixture)
        except (EvalSetError, OSError) as exc:
            say(f"could not write the set, kept in the queue: {exc}")
            summary["skipped"] += 1
            continue
        rows.append(row)
        _remove(directory, path, candidate["id"])
        summary["accepted"] += 1
        say(f"accepted as {row['id']}")
    with mc.locked(directory):
        summary["remaining"] = len(mc.read_queue(path))
    summary["set_rows"] = len(rows)
    return summary


def queue_stats(queue) -> dict:
    directory, path = mc.queue_path(queue)
    if not directory.is_dir():
        raise ReviewError("queue directory not found")
    with mc.locked(directory):
        return mc.stats(mc.read_queue(path))

