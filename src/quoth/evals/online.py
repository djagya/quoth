"""`eval online`: aggregate readout of the sidecar's prefetch log (no text in or out).

A turn is one ``prefetch`` record. A turn counts as a probable miss when the same
session (hash) calls a search tool (``evidence_bundle`` or ``episode_search``) after it
and before its next prefetch: the assistant went looking for something recall did not give it.

``explicit_join`` joins each ``evidence_bundle`` call to its session's last prefetch before
it, note by note (a locator's path): did the search return a note the turn injected, one it
had pooled (judged) but not injected, or one it never pooled?

``late_tail`` is the record-only late-tail measurement of a bounded window (``--since``,
``--until``, ``--max-turns``): each authored turn joined by id to its evidence job's events,
slow and busy apart, completions against the effective deadline, what larger budgets would
have added, durations and headroom inside the client deadline, and the logger's own losses.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from ..prefetch_log import file_day
from .format import STORES
from .metrics import latency, percentile

SCHEMA = "quoth-eval-online/v2"
SEARCH_TOOLS = ("evidence_bundle", "episode_search")
RECORD_TYPES = ("prefetch", "tool", "skip", "start", "submitted", "busy", "wait", "done",
                "instance", "counters")


def read_logs(directory: str | Path, days: int | None = None, today=None) -> tuple[list[dict], dict]:
    """Records of the last `days` UTC days (all files when None), plus a content-free file summary."""
    directory = Path(directory)
    today = today or datetime.now(tz=timezone.utc).date()
    cutoff = today - timedelta(days=days - 1) if days else None
    files = sorted(p for p in directory.glob("prefetch-*.jsonl")
                   if file_day(p.name) and (cutoff is None or file_day(p.name) >= cutoff))
    records, bad = [], 0
    for path in files:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    bad += 1
                    continue
                if isinstance(rec, dict) and rec.get("type") in RECORD_TYPES:
                    records.append(rec)
                else:
                    bad += 1
    records.sort(key=lambda r: str(r.get("ts", "")))
    return records, {"files": len(files), "bad_lines": bad,
                     "first_day": files[0].name[9:19] if files else None,
                     "last_day": files[-1].name[9:19] if files else None}


def _dist(values: list[float]) -> dict:
    return {"n": len(values), "p10": percentile(values, 0.1), "p50": percentile(values, 0.5),
            "p90": percentile(values, 0.9)}


def summarize(records: list[dict]) -> dict:
    turns = [r for r in records if r.get("type") == "prefetch"]
    tools = [r for r in records if r.get("type") == "tool"]
    skipped = sum(1 for r in records if r.get("type") == "skip")
    n = len(turns)

    def rate(count: int) -> float | None:
        return round(count / n, 4) if n else None

    injected = Counter()
    statuses: dict = defaultdict(Counter)
    top1: dict = defaultdict(list)
    injected_scores: dict = defaultdict(list)
    empty = 0
    for t in turns:
        stores = t.get("stores") or {}
        any_injected = False
        for store in STORES:
            items = stores.get(store) or []
            if any(i.get("injected") for i in items):
                injected[store] += 1
                any_injected = True
            scored = [i["score"] for i in items if isinstance(i.get("score"), (int, float))]
            if scored:
                top1[store].append(max(scored))
            injected_scores[store] += [i["score"] for i in items
                                       if i.get("injected") and isinstance(i.get("score"), (int, float))]
        empty += not any_injected
        for store, st in (t.get("status") or {}).items():
            statuses[store][st] += 1
    # Probable misses: a search tool call in the same session after a turn, before the next turn.
    by_session: dict = defaultdict(list)
    for r in records:
        if r.get("session"):
            by_session[r["session"]].append(r)
    missed = Counter()
    for events in by_session.values():
        for i, rec in enumerate(events):
            if rec.get("type") != "prefetch":
                continue
            for nxt in events[i + 1:]:
                if nxt.get("type") == "prefetch":
                    break
                if nxt.get("tool") in SEARCH_TOOLS:
                    missed[nxt["tool"]] += 1
                    missed["any"] += 1
                    break
    return {
        "schema": SCHEMA,
        "turns": n,
        "explicit_join": explicit_join(by_session),
        "skipped_not_authored": skipped,
        "sessions": len({t.get("session") for t in turns if t.get("session")}),
        "versions": dict(Counter(str(r.get("v")) for r in turns)),
        "tool_calls": dict(Counter(str(t.get("tool")) for t in tools)),
        "tool_calls_without_session": sum(1 for t in tools if not t.get("session")),
        "injected_rate": {store: rate(injected[store]) for store in STORES},
        "empty_recall_rate": rate(empty),
        "probable_miss_rate": rate(missed["any"]),
        "probable_miss_by_tool": {tool: rate(missed[tool]) for tool in SEARCH_TOOLS},
        "statuses": {store: dict(c) for store, c in sorted(statuses.items())},
        "scores": {store: {"top1": _dist(top1[store]), "injected": _dist(injected_scores[store])}
                   for store in STORES},
        "query_len": _dist([t["query_len"] for t in turns if isinstance(t.get("query_len"), int)]),
        "latency_ms": latency([t.get("ms") or {} for t in turns]),
    }


def online(directory: str | Path, days: int | None = None, *, since=None, until=None,
           max_turns: int | None = None) -> dict:
    """The readout of the log files (the last `days` UTC days), bounded to a window when
    `since`, `until` or `max_turns` is given: the aggregate covers the window's turns (by
    request id) and the other records inside its time bounds; `late_tail` joins the window's
    turns to their events anywhere in the files read, so a job finishing after the window
    is reconciled, not lost."""
    records, files = read_logs(directory, days)
    window = select_window(records, since, until, max_turns)
    bounded = since is not None or until is not None or max_turns is not None
    if bounded:
        reqs = set(window["reqs"])
        records_in = [r for r in records if (r["req"] in reqs if r.get("req") else in_window(r, window))]
    else:
        records_in = records
    source = {"kind": "sidecar prefetch log", **files,
              "records": dict(sorted(Counter(r["type"] for r in records).items()))}
    return {**summarize(records_in),
            "late_tail": {"source": source, **late_tail_report(records, window)},
            "window": {"days": days, **files, "since": since, "until": until, "max_turns": max_turns}}


def _note(locator) -> str | None:
    """A locator's note path ("a/b.md:3-9" -> "a/b.md")."""
    if not isinstance(locator, str) or ":" not in locator:
        return None
    return locator.rsplit(":", 1)[0]


def _turn_notes(turn: dict) -> dict[str, set]:
    vault = (turn.get("stores") or {}).get("vault") or []
    notes = {"injected": set(), "passed": set(), "pooled": set(), "candidates": set()}
    for item in vault:
        note = _note(item.get("locator"))
        if note is None:
            continue
        notes["candidates"].add(note)
        if item.get("pool"):
            notes["pooled"].add(note)
        if item.get("passed"):
            notes["passed"].add(note)
        if item.get("injected"):
            notes["injected"].add(note)
    return notes


def explicit_join(by_session: dict) -> dict:
    """Per `evidence_bundle` call after a prefetch in its session: how its result notes
    relate to that turn's vault candidates (rates over the joined calls)."""
    calls = 0
    flags = Counter()
    notes = Counter()
    for events in by_session.values():
        last = None
        for rec in events:
            if rec.get("type") == "prefetch":
                last = _turn_notes(rec)
                continue
            if rec.get("type") != "tool" or rec.get("tool") != "evidence_bundle" or last is None:
                continue
            calls += 1
            seen = {n for n in (_note(x) for x in rec.get("results") or []) if n is not None}
            kinds = set()
            for note in seen:
                if note in last["injected"]:
                    kind = "injected"
                elif note in last["passed"]:
                    kind = "passed_not_injected"
                elif note in last["pooled"]:
                    kind = "pooled_not_passed"
                elif note in last["candidates"]:
                    kind = "candidate_not_pooled"
                else:
                    kind = "never_candidate"
                notes[kind] += 1
                kinds.add(kind)
            for kind in kinds:
                flags[kind] += 1
            if kinds & {"passed_not_injected", "pooled_not_passed"}:
                flags["pooled_not_injected"] += 1
            if kinds & {"candidate_not_pooled", "never_candidate"}:
                flags["never_pooled"] += 1
    kinds = ("injected", "passed_not_injected", "pooled_not_passed", "candidate_not_pooled",
             "never_candidate", "pooled_not_injected", "never_pooled")
    return {"calls": calls,
            "rate": {k: (round(flags[k] / calls, 4) if calls else None) for k in kinds},
            "notes": {k: notes[k] for k in kinds[:5]}}


# ------------------------------------------------------------------ late-tail window report

LATE_BUDGETS = (1.5, 2.0, 2.5, 2.8)   # evidence budgets (s) the late-tail report replays
CLIENT_DEADLINE_MS = 3000.0           # the Hermes plugin's own timeout
DEADLINE_BINS = (0, 500, 1000, 1300, 1500)  # completion minus the effective deadline (ms)
OVERLAP_CAPTION = ("Descriptive join only: the next explicit search may look for something other "
                   "than what the turn looked for. This is not answer quality, nor the usefulness "
                   "of any answer.")
BUDGET_NOTE = ("Counts over the window's slow turns. A budget counts from the turn's wait start, "
               "like the live one. Zero cases mean too little data, not that there is no problem; "
               "this report alone makes no budget safe.")


def _when(value) -> datetime | None:
    """An ISO-8601 time (a date alone is midnight UTC; no zone means UTC), else None."""
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _stats(values: list[float]) -> dict:
    return {"n": len(values), "p50": percentile(values, 0.5), "p90": percentile(values, 0.9),
            "max": max(values) if values else None}


def _bins(values: list[float]) -> dict:
    edges = DEADLINE_BINS
    labels = [f"<={edges[0]}"] + [f"{a}-{b}" for a, b in zip(edges, edges[1:])] + [f">{edges[-1]}"]
    out = dict.fromkeys(labels, 0)
    for v in values:
        i = sum(v > e for e in edges)  # 0: on or before the deadline
        out[labels[i]] += 1
    return out


def select_window(records: list[dict], since=None, until=None, max_turns: int | None = None) -> dict:
    """The authored turns (`start` lines) of a bounded window: started at or after `since`,
    before `until`, the first `max_turns` of them. Returns their ids and the bounds; `end`
    is where the window closes (`until`, or the last turn's start when `max_turns` cut it)."""
    lo = _when(since) if since else None
    hi = _when(until) if until else None
    starts = []
    for r in records:
        if r.get("type") != "start" or not r.get("req"):
            continue
        ts = _when(r.get("ts"))
        if ts is None or (lo is not None and ts < lo) or (hi is not None and ts >= hi):
            continue
        starts.append((ts, r))
    starts.sort(key=lambda x: x[0])
    truncated = max_turns is not None and len(starts) > max_turns
    if max_turns is not None:
        starts = starts[:max_turns]
    end = starts[-1][0] if truncated and starts else hi
    return {"reqs": [r["req"] for _, r in starts], "since": lo, "until": hi, "end": end,
            "end_inclusive": truncated, "max_turns": max_turns, "truncated": truncated,
            "first_start": starts[0][1]["ts"] if starts else None,
            "last_start": starts[-1][1]["ts"] if starts else None}


def in_window(rec: dict, window: dict) -> bool:
    """A record's `ts` falls inside the window's bounds."""
    ts = _when(rec.get("ts"))
    if ts is None:
        return False
    if window["since"] is not None and ts < window["since"]:
        return False
    end = window["end"]
    if end is not None and (ts > end if window["end_inclusive"] else ts >= end):
        return False
    return True


def late_tail_report(records: list[dict], window: dict) -> dict:
    """The record-only late-tail measurement of one window (see docs/EVAL.md).

    Every turn of the window is joined by id to its events, wherever they sit in the log:
    a job started in the window and finished after it is reconciled (`completed_after_window`),
    and a turn missing a line is counted as missing, never dropped from a denominator."""
    reqs = window["reqs"]
    wanted = set(reqs)
    by_req: dict = defaultdict(dict)
    done_by_job: dict = {}
    duplicates = Counter()
    for r in records:
        kind, req = r.get("type"), r.get("req")
        if kind == "done" and r.get("job"):
            if r["job"] in done_by_job:
                duplicates["done"] += 1
            done_by_job.setdefault(r["job"], r)
        elif req in wanted and kind in ("start", "submitted", "busy", "wait", "prefetch"):
            if kind in by_req[req]:
                duplicates[kind] += 1
            by_req[req].setdefault(kind, r)
    status, strata = Counter(), {}
    outcomes, exc_types, missing = Counter(), Counter(), Counter()
    after_window = over_client = 0
    slow_vs_deadline, all_vs_deadline = [], []
    request_ms, job_ms, headroom = [], [], []
    budgets = {f"{b:g}s": Counter() for b in LATE_BUDGETS}
    busy = {"n": 0, "linked_to_job": 0, "linked_job_in_window": 0}
    jobs_of_window: dict = {}
    busy_links = []
    turns = []
    for req in reqs:
        ev = by_req.get(req, {})
        start, ret, wait = ev.get("start") or {}, ev.get("prefetch"), ev.get("wait") or {}
        job = (ev.get("submitted") or {}).get("job") or (ret or {}).get("job")
        done = done_by_job.get(job) if job else None
        st = ((ret or {}).get("status") or {}).get("vault") if ret else None
        if ret is None:
            missing["return"] += 1
            st = "busy" if "busy" in ev else "missing_return"
        status[st] += 1
        if job:
            jobs_of_window[job] = req
            missing["wait"] += "wait" not in ev
            if done is None:
                missing["done"] += 1
            else:
                outcomes[done.get("outcome")] += 1
                if done.get("outcome") == "exception":
                    exc_types[str(done.get("exc_type"))] += 1
                done_ts = _when(done.get("ts"))
                if window["end"] is not None and done_ts is not None and done_ts > window["end"]:
                    after_window += 1
        if st == "busy":
            busy["n"] += 1
            linked = (ev.get("busy") or {}).get("busy_job") or (ret or {}).get("busy_job")
            if linked:
                busy["linked_to_job"] += 1
                busy_links.append(linked)
        key = (start.get("v"), start.get("code_sha256"), start.get("config_sha256"),
               (done or {}).get("snapshot_id") or (ret or {}).get("snapshot_id"))
        stratum = strata.setdefault(key, Counter())
        stratum["turns"] += 1
        stratum[st] += 1
        if ret is not None and _num(ret.get("returned_ms")):
            request_ms.append(ret["returned_ms"])
            headroom.append(round(CLIENT_DEADLINE_MS - ret["returned_ms"], 2))
            over_client += ret["returned_ms"] > CLIENT_DEADLINE_MS
        normal = done is not None and done.get("outcome") == "normal" and _num(done.get("completed_ms"))
        if normal and _num((ev.get("submitted") or {}).get("submitted_ms")):
            job_ms.append(round(done["completed_ms"] - ev["submitted"]["submitted_ms"], 2))
        if normal and _num(wait.get("deadline_ms")):
            since_deadline = round(done["completed_ms"] - wait["deadline_ms"], 2)
            all_vs_deadline.append(since_deadline)
            if st == "slow":
                slow_vs_deadline.append(since_deadline)
        if st == "slow":
            _replay_budgets(budgets, done, wait, ret)
        turns.append({"status": st, "session": start.get("session"), "ts": start.get("ts"),
                      "done": done, "prefetch": ret})
    busy["linked_job_in_window"] = sum(j in jobs_of_window for j in busy_links)
    fields = ("fit", "admissible_nonempty", "admissible_nonempty_within_client_deadline", "empty_raw",
              "empty_post_gate", "unknown", "not_fit", "exception", "censored", "missing")
    return {
        "window": {"since": window["since"].isoformat() if window["since"] else None,
                   "until": window["until"].isoformat() if window["until"] else None,
                   "max_turns": window["max_turns"], "truncated": window["truncated"],
                   "first_start": window["first_start"], "last_start": window["last_start"]},
        "turns": len(reqs),
        "status": dict(status),
        "strata": [{"v": k[0], "code_sha256": k[1], "config_sha256": k[2], "snapshot_id": k[3], **dict(c)}
                   for k, c in sorted(strata.items(), key=lambda kv: tuple(str(x) for x in kv[0]))],
        "jobs": {"started": len(jobs_of_window), "outcomes": dict(outcomes),
                 "exception_types": dict(exc_types), "completed_after_window": after_window},
        "missing": {k: missing[k] for k in ("return", "wait", "done")},
        "duplicates": dict(duplicates),
        "logger": _logger_counters(records),
        "slow": {"n": status["slow"], "completion_minus_deadline_ms": _stats(slow_vs_deadline),
                 "bins_ms": _bins(slow_vs_deadline)},
        "busy": busy,
        "completion_minus_deadline_ms": _stats(all_vs_deadline),
        "budgets": {"denominator_slow_turns": status["slow"], "note": BUDGET_NOTE,
                    "by_budget": {label: {k: c[k] for k in fields} for label, c in budgets.items()}},
        "durations_ms": {"request": _stats(request_ms), "job": _stats(job_ms),
                         "headroom_to_client_deadline": {
                             "n": len(headroom), "p50": percentile(headroom, 0.5),
                             "p10": percentile(headroom, 0.1), "min": min(headroom) if headroom else None},
                         "over_client_deadline": over_client, "client_deadline_ms": CLIENT_DEADLINE_MS},
        "explicit_overlap": _explicit_overlap(turns, records),
    }


def _replay_budgets(budgets: dict, done: dict | None, wait: dict, ret: dict | None) -> None:
    """One slow turn under each budget B: did its job finish within B of the wait start, and
    would it then have injected something (admissible nonempty), nothing, or unknown?"""
    for b in LATE_BUDGETS:
        c = budgets[f"{b:g}s"]
        outcome = (done or {}).get("outcome")
        if done is None or not (_num(wait.get("wait_ms")) or outcome != "normal"):
            c["missing"] += 1
            continue
        if outcome in ("exception", "censored"):
            c[outcome] += 1
            continue
        if not _num(done.get("completed_ms")):
            c["missing"] += 1
            continue
        if done["completed_ms"] - wait["wait_ms"] > b * 1000:
            c["not_fit"] += 1
            continue
        c["fit"] += 1
        if done.get("decision") != "computed":
            c["unknown"] += 1
        elif not done.get("raw_n"):
            c["empty_raw"] += 1
        elif not done.get("would_inject_n"):
            c["empty_post_gate"] += 1
        else:
            c["admissible_nonempty"] += 1
            # The turn as it would have returned: its post-wait work after the later finish.
            if ret is not None and _num(ret.get("returned_ms")) and _num(ret.get("wait_end_ms")):
                projected = ret["returned_ms"] - ret["wait_end_ms"] + done["completed_ms"]
                c["admissible_nonempty_within_client_deadline"] += projected <= CLIENT_DEADLINE_MS


def _logger_counters(records: list[dict]) -> dict:
    """The logger's own counters: the last line per process instance, summed. An instance
    with no `close` line ended without a clean shutdown (its last counters may be stale)."""
    last, closed, instances = {}, set(), set()
    for r in records:
        if r.get("type") == "instance" and r.get("inst"):
            instances.add(r["inst"])
        elif r.get("type") == "counters" and r.get("inst"):
            last[r["inst"]] = r
            if r.get("reason") == "close":
                closed.add(r["inst"])
    keys = ("dropped", "errors", "jobs_started", "jobs_completed", "jobs_censored", "in_flight",
            "unjoined_completions", "untracked")
    return {"instances": len(instances | set(last)), "instances_with_counters": len(last),
            "instances_closed": len(closed),
            **{k: sum(int(r.get(k) or 0) for r in last.values()) for k in keys}}


def _explicit_overlap(turns: list[dict], records: list[dict]) -> dict:
    """Per turn: the session's next `evidence_bundle` call (before its next turn), note by
    note against what the turn injected (ok) or what its late job would have injected (slow)."""
    tools: dict = defaultdict(list)
    starts: dict = defaultdict(list)
    for r in records:
        if not r.get("session"):
            continue
        if r.get("type") == "tool" and r.get("tool") == "evidence_bundle":
            tools[r["session"]].append(r)
        elif r.get("type") == "start":
            starts[r["session"]].append(_when(r.get("ts")))
    out = {"caption": OVERLAP_CAPTION}
    for label in ("ok_injected", "slow_would_inject"):
        out[label] = {"turns": 0, "followed_by_search": 0, "overlap": 0}
    for t in turns:
        if t["status"] == "ok" and t["prefetch"]:
            label = "ok_injected"
            notes = {_note(i.get("locator")) for i in (t["prefetch"].get("stores") or {}).get("vault") or []
                     if i.get("injected")}
        elif t["status"] == "slow" and t["done"] and t["done"].get("decision") == "computed":
            label = "slow_would_inject"
            notes = {_note(x) for x in t["done"].get("would_inject") or []}
        else:
            continue
        notes.discard(None)
        ts = _when(t["ts"])
        if not notes or not t["session"] or ts is None:
            continue
        out[label]["turns"] += 1
        nxt = min((s for s in starts[t["session"]] if s is not None and s > ts), default=None)
        call = next((c for c in tools[t["session"]] if (_when(c.get("ts")) or ts) > ts
                     and (nxt is None or _when(c.get("ts")) < nxt)), None)
        if call is None:
            continue
        out[label]["followed_by_search"] += 1
        got = {n for n in (_note(x) for x in call.get("results") or []) if n}
        out[label]["overlap"] += bool(notes & got)
    return out
