"""`eval online`: aggregate readout of the sidecar's prefetch log (no text in or out).

A turn is one ``prefetch`` record. A turn counts as a probable miss when the same
session (hash) calls a search tool (``evidence_bundle`` or ``episode_search``) after it
and before its next prefetch: the assistant went looking for something recall did not give it.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from ..prefetch_log import file_day
from .format import STORES
from .metrics import latency, percentile

SCHEMA = "quoth-eval-online/v1"
SEARCH_TOOLS = ("evidence_bundle", "episode_search")


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
                if isinstance(rec, dict) and rec.get("type") in ("prefetch", "tool", "skip"):
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


def online(directory: str | Path, days: int | None = None) -> dict:
    records, files = read_logs(directory, days)
    return {**summarize(records), "window": {"days": days, **files}}
