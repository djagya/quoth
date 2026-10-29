"""The control lab's E1 temporal/abstention and Q2 facet scorers (lab-only).

Moved from the retired ``eval_temporal.py`` / ``eval_quality.py`` experiment runners: the
lab is their only caller. They score a pipeline spec's evidence bundle for chronological
answers, provenance and snapshot-bound excerpt facets, which eval set v1 and the prefetch
harness do not express (see docs/EVAL.md, "Control lab"). Ordinary path retrieval is
scored with the harness metrics (`quoth.evals.gate.path_scores`).

validate: checks every E1 gold item against the DB by a chronological lexical scan of the
          validated dated series (independent of dense retrieval).
"""

from __future__ import annotations

import json
import re
import statistics
from datetime import date


_FN = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def validated_note_date(path: str, metadata: str | None) -> date | None:
    """Accept note time only when filename date equals frontmatter ``created``."""
    m = _FN.match(path.rsplit("/", 1)[-1])
    if not m:
        return None
    try:
        created = str((json.loads(metadata) if metadata else {}).get("created") or "")[:10]
    except (TypeError, ValueError):
        return None
    if created != m.group(1):
        return None
    try:
        return date.fromisoformat(created)
    except ValueError:
        return None


def _series_notes(conn, series):
    """{path: (note_date, [(chunk_id, line_start, line_end, text)])} for validated dated notes."""
    out = {}
    for r in conn.execute(
            "SELECT s.path, v.metadata, c.id, c.line_start, c.line_end, c.text FROM sources s"
            " JOIN source_versions v ON v.source_id=s.id AND v.is_current=1"
            " JOIN chunks c ON c.source_version_id=v.id WHERE s.path LIKE ? ORDER BY c.id",
            (series + "%",)):
        if "/" in r[0][len(series):]:
            continue
        nd = validated_note_date(r[0], r[1])
        if nd is None:
            continue
        out.setdefault(r[0], (nd, []))[1].append((r[2], r[3], r[4], r[5]))
    return out


def _in(nd, lo, hi):
    return (lo is None or nd >= date.fromisoformat(lo)) and (hi is None or nd <= date.fromisoformat(hi))


def validate(conn, items):
    problems = []
    for it in items:
        if it["category"] in ("ordinary", "authority"):
            for p in it["gold_paths"]:
                if not conn.execute("SELECT 1 FROM sources s JOIN source_versions v ON v.source_id=s.id"
                                    " AND v.is_current=1 WHERE s.path=?", (p,)).fetchone():
                    problems.append((it["id"], "missing current gold path", p))
            for f in it.get("controls", []):
                for cid, phrase in f["requires"]:
                    row = conn.execute("SELECT text FROM chunks WHERE id=?", (cid,)).fetchone()
                    if not row or phrase not in row[0]:
                        problems.append((it["id"], "control phrase absent", cid))
            continue
        notes = _series_notes(conn, it["series"])
        if not notes:
            problems.append((it["id"], "empty validated series", it["series"]))
            continue
        if it.get("evidence_regex"):
            rx = re.compile(it["evidence_regex"])
            hits = sorted((nd, p) for p, (nd, ch) in notes.items()
                          if _in(nd, it.get("lo"), it.get("hi")) and any(rx.search(c[3]) for c in ch))
        else:
            acc = set(it["accept_chunks"])
            hits = sorted((nd, p) for p, (nd, ch) in notes.items()
                          if _in(nd, it.get("lo"), it.get("hi")) and any(c[0] in acc for c in ch))
        if it["expect_abstain"]:
            if hits:
                problems.append((it["id"], "abstain item has evidence", hits[:3]))
            continue
        if not hits:
            problems.append((it["id"], "no evidence for gold", it["gold_path"]))
            continue
        want = {"first": hits[0][1], "last": hits[-1][1]}.get(it["op"])
        if want is not None and want != it["gold_path"]:
            problems.append((it["id"], "gold is not the chronological extreme", want))
        if it["op"] == "event" and it["gold_path"] not in {p for _, p in hits}:
            problems.append((it["id"], "event gold path lacks evidence", it["gold_path"]))
    return problems


def _fn_date(path):
    m = _FN.match(path.rsplit("/", 1)[-1])
    try:
        return date.fromisoformat(m.group(1)) if m else None
    except ValueError:
        return None


def baseline_caller(bundle, it):
    """Deterministic chronological caller over a dense bundle: the best a caller can do
    from paths alone. Picks the extreme filename-dated hit inside the stated series/window."""
    if not it.get("op") or it["op"] == "event":
        return None
    lo = date.fromisoformat(it["lo"]) if it.get("lo") else None
    hi = date.fromisoformat(it["hi"]) if it.get("hi") else None
    cand = []
    for h in bundle["hits"]:
        if not h["path"].startswith(it["series"]) or "/" in h["path"][len(it["series"]):]:
            continue
        nd = _fn_date(h["path"])
        if nd is None or (lo and nd < lo) or (hi and nd > hi):
            continue
        cand.append((nd, -h["rank"], h))
    if not cand:
        return {"status": "insufficient"}
    pick = (max if it["op"] == "last" else min)(cand, key=lambda t: (t[0], t[1]))
    return {"status": "answered", "path": pick[2]["path"], "note_date": pick[0].isoformat(),
            "locator": pick[2]["locator"], "line_start": pick[2]["line_start"],
            "line_end": pick[2]["line_end"]}


def _chunk_supports(conn, it, ans):
    """Provenance: cited locator is an exact current chunk that carries the gold evidence."""
    row = conn.execute(
        "SELECT c.id, c.text FROM chunks c JOIN source_versions v ON v.id=c.source_version_id"
        " AND v.is_current=1 JOIN sources s ON s.id=v.source_id"
        " WHERE s.path=? AND c.line_start=? AND c.line_end=?",
        (ans["path"], ans["line_start"], ans["line_end"])).fetchone()
    if row is None:
        return False
    if it.get("accept_chunks"):
        return row[0] in set(it["accept_chunks"])
    return bool(re.search(it["evidence_regex"], row[1]))


def _ranks(bundle, paths):
    for h in bundle["hits"]:
        if h["path"] in paths:
            return h["rank"]
    return None


def _controls(bundle, controls, conn):
    locs = {(h["path"], h["line_start"], h["line_end"]) for h in bundle["hits"]}
    out = []
    for f in controls:
        ok = True
        for cid, _ in f["requires"]:
            r = conn.execute("SELECT s.path, c.line_start, c.line_end FROM chunks c JOIN source_versions v"
                             " ON v.id=c.source_version_id JOIN sources s ON s.id=v.source_id WHERE c.id=?",
                             (cid,)).fetchone()
            ok &= tuple(r) in locs
        out.append(ok)
    return out


def score_item(conn, it, bundle, ans, applied):
    r = {"id": it["id"], "category": it["category"], "lang": it["lang"], "applied": applied}
    gold = [it["gold_path"]] if it.get("gold_path") else it.get("gold_paths", [])
    rank = _ranks(bundle, set(gold)) if gold else None
    r["gold_rank"] = rank
    if it["category"] == "authority":
        r["controls"] = _controls(bundle, it["controls"], conn)
    if ans is not None:
        r["answer"] = ans
        if it["expect_abstain"]:
            r["answer_correct"] = ans["status"] == "insufficient"
        else:
            r["answer_correct"] = ans["status"] == "answered" and ans["path"] == it["gold_path"]
            r["temporal_correct"] = bool(r["answer_correct"] and ans.get("note_date") == str(_fn_date(it["gold_path"])))
            r["provenance_correct"] = bool(r["answer_correct"] and _chunk_supports(conn, it, ans))
    r["assistant_hits"] = sum(h["path"].startswith("ASSISTANT/") for h in bundle["hits"])
    return r


def facet_results(hits, controls):
    """Q2: every required excerpt (chunk id + decisive phrase) is in the bounded bundle text."""
    by_id = {h['chunk_id']: h['snippet'][:1800] for h in hits}
    return {c['name']: all(needle in by_id.get(cid, '') for cid, needle in c['requires'])
            for c in controls}


def _rate(xs):
    xs = list(xs)
    return round(sum(xs) / len(xs), 4) if xs else None


def summarize(rows):
    by = {}
    for cat in sorted({r["category"] for r in rows}):
        rs = [r for r in rows if r["category"] == cat]
        d = {"n": len(rs)}
        ranked = [r for r in rs if r["gold_rank"] is not None or r["category"] != "abstain_temporal"]
        if cat != "abstain_temporal":
            for kk in (1, 5, 10, 15):
                d[f"recall@{kk}"] = _rate(r["gold_rank"] is not None and r["gold_rank"] <= kk for r in ranked)
            d["mrr"] = round(sum(1 / r["gold_rank"] for r in ranked if r["gold_rank"]) / len(ranked), 4)
        if any("answer_correct" in r for r in rs):
            d["answer_correct"] = _rate(r.get("answer_correct", False) for r in rs)
        if any("temporal_correct" in r for r in rs):
            d["temporal_correct"] = _rate(r.get("temporal_correct", False) for r in rs)
            d["provenance_correct"] = _rate(r.get("provenance_correct", False) for r in rs)
        if cat == "authority":
            d["control_facets_pass"] = _rate(all(r["controls"]) for r in rs)
        d["applied"] = sum(r["applied"] for r in rs)
        by[cat] = d
    abst = [r for r in rows if r.get("answer", {}).get("status") == "insufficient"]
    should = [r for r in rows if r["category"] == "abstain_temporal"]
    ms = sorted(r["ms"] for r in rows)
    return {"by_category": by,
            "abstention": {"abstained": len(abst),
                           "precision": _rate(r["category"] == "abstain_temporal" for r in abst),
                           "recall": _rate(r.get("answer_correct", False) for r in should)},
            "latency_ms": {"p50": statistics.median(ms), "p95": ms[max(0, int(round(0.95 * len(ms))) - 1)],
                           "max": ms[-1]},
            "assistant_hits": sum(r["assistant_hits"] for r in rows),
            "max_bundle_chars": max(r["bundle_chars"] for r in rows)}
