"""Opt-in miss capture (`--capture-misses`) and its review queue (`quoth eval review`)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
import json
import os
import time

import pytest

from quoth import miss_capture as mc
from quoth import relevance, sidecar
from quoth.evals import cli as eval_cli
from quoth.evals import review as rv
from quoth.evals.format import load_set, validate, write_set
from quoth.prefetch_log import PrefetchLog
from test_sidecar import snapshot_db  # noqa: F401  (fixture)

QUESTION = "when is the zucchini harvest planned"
SEARCH = "zucchini harvest date garden"


class FakeEvidence:
    """Prefetch gets `turn_hits`; the assistant's own search gets `search_hits`."""

    def __init__(self, turn_hits=(), search_hits=()):
        self.turn_hits = list(turn_hits)
        self.search_hits = list(search_hits)

    def bundle(self, query=None, k=10, model="m", *, path=None, path_prefix=None, include_explicit=None, **kw):
        if include_explicit is None:  # the prefetch evidence worker
            return {"hits": [dict(h) for h in self.turn_hits], "provenance": {"snapshot_id": "snap1"}}
        if path is not None:
            return {"hits": [{"locator": f"{path}:1-2", "path": path, "text": "note body"}],
                    "provenance": {"snapshot_id": "snap1"}}
        return {"hits": [dict(h) for h in self.search_hits], "provenance": {"snapshot_id": "snap1"}}


def hit(path, cosine=0.8, text="zucchini harvest in the garden"):
    return {"locator": f"{path}:1-4", "path": path, "text": text, "cosine": cosine, "note_date": None}


def make_app(tmp_path, evidence, gate=None, plog=False, **capture):
    cap = mc.MissCapture(tmp_path / "misses", **capture)
    log = PrefetchLog(tmp_path / "plog") if plog else None
    app = sidecar.Quoth(ledger_path=tmp_path / "l.db", state_db=None, evidence=evidence,
                        gate=relevance.Gate() if gate is None else gate, miss_capture=cap, prefetch_log=log,
                        evidence_budget=5.0)
    return app, cap


def entries(cap):
    cap.flush()
    return mc.read_queue(cap.path)


def search(app, session="s1", query=SEARCH, **body):
    return app.evidence_bundle({"query": query, "k": 5, "session_id": session, **body})


def test_searched_after_empty_turn_is_captured_with_gold_candidates(tmp_path):
    ev = FakeEvidence(search_hits=[hit("PROJECTS/Garden.md"), hit("PROJECTS/Garden.md"), hit("JOURNAL/Day.md")])
    app, cap = make_app(tmp_path, ev, plog=True)
    try:
        out = app.prefetch({"query": QUESTION, "session_id": "s1"})
        assert out["evidence"] == 0
        search(app)
        got = entries(cap)
        assert len(got) == 1
        e = got[0]
        assert e["signal"] == "searched_after_empty" and e["question"] == QUESTION
        assert e["injected"] == [] and e["injected_paths"] == [] and e["claims_injected"] == []
        assert e["searches"][0]["query"] == SEARCH
        assert e["searches"][0]["paths"] == ["PROJECTS/Garden.md", "JOURNAL/Day.md"]
        assert e["snapshot_id"] == "snap1" and e["v"] and e["vault_status"] == "ok"
        assert e["schema"] == mc.SCHEMA and len(e["id"]) == 16
        health = app.health()["miss_capture"]
        assert health["written"] == 1 and health["errors"] == health["dropped"] == 0
        assert "zucchini" not in json.dumps(app.health())
        # Private files: directory 0700, queue 0600.
        assert oct(cap.dir.stat().st_mode & 0o777) == "0o700"
        assert oct(cap.path.stat().st_mode & 0o777) == "0o600"
        # The prefetch log never gets the question or the search text.
        app.prefetch_log.flush()
        raw = "".join(p.read_text() for p in (tmp_path / "plog").glob("*.jsonl"))
        assert raw and "zucchini" not in raw and QUESTION not in raw
    finally:
        app.close()


def test_search_despite_injection_and_gate_rejected_pool(tmp_path):
    ev = FakeEvidence(turn_hits=[hit("PROJECTS/Garden.md")], search_hits=[hit("PROJECTS/Plan.md")])
    app, cap = make_app(tmp_path, ev)
    try:
        assert app.prefetch({"query": QUESTION, "session_id": "s1"})["evidence"] == 1
        search(app)
        (e,) = entries(cap)
        assert e["signal"] == "searched_despite_injection"
        assert e["injected"] == ["PROJECTS/Garden.md:1-4"] and e["injected_paths"] == ["PROJECTS/Garden.md"]
    finally:
        app.close()
    ev = FakeEvidence(turn_hits=[hit("PROJECTS/Garden.md", cosine=0.2)], search_hits=[hit("PROJECTS/Plan.md")])
    app, cap = make_app(tmp_path / "b", ev, gate=relevance.Gate(vault_min_cosine=0.9))
    try:
        assert app.prefetch({"query": QUESTION, "session_id": "s1"})["evidence"] == 0
        search(app)
        (e,) = entries(cap)
        assert e["signal"] == "gate_rejected_pool" and e["pool_n"] == 1 and e["passed_n"] == 0
    finally:
        app.close()


def test_only_a_search_in_the_open_window_of_the_same_session_counts(tmp_path):
    ev = FakeEvidence(search_hits=[hit("PROJECTS/Garden.md")])
    app, cap = make_app(tmp_path, ev, window=0.2)
    try:
        search(app)                                        # no turn yet
        app.prefetch({"query": QUESTION, "session_id": "s1"})
        search(app, session="s2")                          # another session
        app.evidence_bundle({"path": "PROJECTS/Garden.md", "session_id": "s1"})  # a path fetch
        search(app, dry_run=True)                          # an eval run
        app.prefetch({"query": "something else entirely", "session_id": "s1", "dry_run": True})
        assert entries(cap) == []
        # The session's next turn closes the window: the search belongs to that turn.
        app.prefetch({"query": "second question about beans", "session_id": "s1"})
        search(app)
        assert [e["question"] for e in entries(cap)] == ["second question about beans"]
        # An injected (not authored) turn closes it too.
        app.prefetch({"query": "third question about peas", "session_id": "s1"})
        app.prefetch({"query": "[kanban] wake: task 12 is due", "session_id": "s1"})
        search(app, query="peas")
        # A search after the window is not a miss of the earlier turn.
        app.prefetch({"query": "fourth question about corn", "session_id": "s1"})
        time.sleep(0.3)
        search(app, query="corn")
        assert len(entries(cap)) == 1
    finally:
        app.close()


def test_identical_questions_merge_and_searches_are_bounded(tmp_path):
    ev = FakeEvidence(search_hits=[hit("PROJECTS/Garden.md")])
    app, cap = make_app(tmp_path, ev)
    try:
        app.prefetch({"query": QUESTION, "session_id": "s1"})
        for i in range(mc.MAX_SEARCHES + 2):
            search(app, query=f"{SEARCH} {i}")
        app.prefetch({"query": "  When is the ZUCCHINI harvest planned ", "session_id": "s2"})
        search(app, session="s2", query="another wording")
        (e,) = entries(cap)
        assert e["seen"] == mc.MAX_SEARCHES + 1
        assert len(e["searches"]) == mc.MAX_SEARCHES
        assert cap.written == mc.MAX_SEARCHES + 1
    finally:
        app.close()


def test_retention_caps_entries_and_age(tmp_path):
    now = datetime(2026, 11, 1, tzinfo=timezone.utc)
    old = (now - timedelta(days=31)).isoformat()
    rows = [{"id": f"c{i}", "question": f"q{i}", "ts": (now - timedelta(hours=i)).isoformat()} for i in range(5)]
    rows.append({"id": "old", "question": "old one", "ts": old})
    kept = mc.prune(rows, days=30, max_entries=3, now=now)
    assert [r["id"] for r in kept] == ["c2", "c1", "c0"]
    # The sidecar's writer applies the same rules on every write and at start.
    d = tmp_path / "misses"
    d.mkdir(mode=0o700)
    mc.write_queue(d / mc.QUEUE_FILE, [{"id": "old", "question": "old one", "ts": "2020-01-01T00:00:00+00:00"}])
    cap = mc.MissCapture(d, max_entries=2)
    try:
        assert entries(cap) == []
        for i in range(3):
            cap.turn("s", f"question {i}")
            cap.search("s", "q", [])
        assert [e["question"] for e in entries(cap)] == ["question 1", "question 2"]
    finally:
        cap.close()


def test_wider_permissions_are_repaired_and_symlinks_refused(tmp_path):
    d = tmp_path / "misses"
    d.mkdir()
    os.chmod(d, 0o755)
    (d / mc.QUEUE_FILE).write_text("")
    os.chmod(d / mc.QUEUE_FILE, 0o644)
    cap = mc.MissCapture(d)
    cap.close()
    assert oct(d.stat().st_mode & 0o777) == "0o700"
    assert oct((d / mc.QUEUE_FILE).stat().st_mode & 0o777) == "0o600"
    link = tmp_path / "link"
    link.symlink_to(d)
    with pytest.raises(ValueError):
        mc.MissCapture(link)


def test_a_failing_capture_never_breaks_a_turn(tmp_path, monkeypatch):
    ev = FakeEvidence(search_hits=[hit("PROJECTS/Garden.md")])
    app, cap = make_app(tmp_path, ev)
    monkeypatch.setattr(mc, "write_queue", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    try:
        assert app.prefetch({"query": QUESTION, "session_id": "s1"})["evidence_status"] == "ok"
        assert search(app)["hits"]
        cap.flush()
        assert cap.errors == 1 and cap.written == 0
        monkeypatch.setattr(cap, "_put", lambda item: (_ for _ in ()).throw(RuntimeError("boom")))
        assert search(app)["hits"]
    finally:
        app.close()


def test_sidecar_flag_is_off_by_default_and_wires_the_capture(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(sidecar, "Quoth", lambda **kw: seen.update(kw) or (_ for _ in ()).throw(SystemExit(0)))
    with pytest.raises(SystemExit):
        sidecar.main(["--ledger", ":memory:", "--state-db", ""])
    assert seen["miss_capture"] is None
    with pytest.raises(SystemExit):
        sidecar.main(["--ledger", ":memory:", "--state-db", "", "--capture-misses", str(tmp_path / "m"),
                      "--capture-misses-window", "60", "--capture-misses-max", "10"])
    cap = seen["miss_capture"]
    try:
        assert isinstance(cap, mc.MissCapture) and cap.window == 60 and cap.max_entries == 10
        assert cap.days == mc.DEFAULT_DAYS
    finally:
        cap.close()


# ---------------------------------------------------------------- review

def candidate(cid, question, signal="searched_after_empty", returned=("PROJECTS/Garden.md", "WORK/Quarterly plan.md"),
              injected=()):
    return {"schema": mc.SCHEMA, "id": cid, "ts": "2026-10-30T09:00:00.000+00:00", "last_ts": None,
            "signal": signal, "question": question, "injected": [f"{p}:1-2" for p in injected],
            "injected_paths": list(injected), "claims_injected": [], "lanes_injected": {},
            "vault_status": "ok", "pool_n": 0, "passed_n": 0,
            "searches": [{"query": "assistant query", "paths": list(returned), "after_s": 4.2}],
            "snapshot_id": "snap1", "v": "0.17.1", "seen": 1, "key": mc.question_key(question)}


def queue(tmp_path, *cands):
    d = tmp_path / "misses"
    d.mkdir(mode=0o700, exist_ok=True)
    mc.write_queue(d / mc.QUEUE_FILE, list(cands))
    return d


def scripted(*lines):
    it = iter(lines)

    def ask(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError from None
    return ask


def test_review_loop_turns_candidates_into_valid_private_rows(tmp_path):
    d = queue(tmp_path,
              candidate("aaaa", "which notes cover the garden and the plan"),
              candidate("bbbb", "what did I decide about the boat", signal="searched_despite_injection",
                        injected=("JOURNAL/Session 1.md",)),
              candidate("cccc", "wher is my bloodwork"),
              candidate("dddd", "private thing to throw away"),
              candidate("eeee", "left for later"))
    out = tmp_path / "set" / "real.jsonl"
    said = []
    ask = scripted("1,2", "y", "", "",              # two gold paths as all-of groups, kind fact, lang en
                   "a", "2", "", "",                 # abstain: not_in_vault, kind unanswerable
                   "e", "where is my bloodwork", "HEALTH/Bloodwork.md", "temporal", "",  # edit, typed path
                   "r",                              # reject
                   "s")                              # skip
    summary = rv.review(d, out, ask=ask, say=said.append)
    assert summary == {"pending": 5, "accepted": 3, "rejected": 1, "skipped": 1, "already_in_set": 0,
                       "remaining": 1, "set_rows": 3}
    assert validate(out) == []
    s = load_set(out)
    assert s.manifest["privacy"] == "private" and oct(out.stat().st_mode & 0o777) == "0o600"
    a, b, c = s.rows
    assert a["origin"] == b["origin"] == c["origin"] == "real_user"
    assert a["expect"]["sources"] == [{"store": "vault", "match": {"path": "PROJECTS/Garden.md"}},
                                      {"store": "vault", "match": {"path": "WORK/Quarterly plan.md"}}]
    assert [g["name"] for g in a["expect"]["groups"]] == ["g1", "g2"]
    assert a["provenance"]["author"] == "owner" and a["provenance"]["review"] == "accepted"
    assert a["provenance"]["mined_from"] == "miss-capture:searched_after_empty" and a["asked_at"]
    assert b["expect"] == {"abstain": True, "sources": []} and b["abstain_reason"] == "not_in_vault"
    assert b["kind"] == "unanswerable" and "signal:searched_despite_injection" in b["tags"]
    assert c["question"] == "where is my bloodwork" and c["provenance"]["review"] == "edited"
    assert c["kind"] == "temporal" and c["expect"]["sources"][0]["match"]["path"] == "HEALTH/Bloodwork.md"
    assert c["meta"] == {"snapshot_id": "snap1", "quoth_version": "0.17.1"}
    # Reviewed and rejected candidates are gone from the queue, text included.
    raw = (d / mc.QUEUE_FILE).read_text()
    assert [e["id"] for e in mc.read_queue(d / mc.QUEUE_FILE)] == ["eeee"]
    assert "throw away" not in raw and "garden" not in raw
    assert any("1. PROJECTS/Garden.md  (returned)" in line for line in said)
    # A second session appends to the same set and keeps its manifest valid.
    assert rv.review(d, out, ask=scripted("2"), say=said.append)["accepted"] == 0  # EOF mid-answer: nothing written
    assert rv.review(d, out, ask=scripted("2", "", ""), say=said.append)["accepted"] == 1
    assert validate(out) == [] and len(load_set(out).rows) == 4


def test_review_quit_eof_and_bad_input_keep_candidates(tmp_path):
    d = queue(tmp_path, candidate("aaaa", "first"), candidate("bbbb", "second"))
    out = tmp_path / "real.jsonl"
    said = []
    summary = rv.review(d, out, ask=scripted("9", "../etc/x.md", "/abs.md", "", "q"), say=said.append)
    assert summary["accepted"] == 0 and summary["remaining"] == 2 and not out.exists()
    assert "no path numbered 9" in said and any("not a vault-relative path" in s for s in said)
    assert rv.review(d, out, ask=scripted(), say=said.append)["remaining"] == 2  # EOF = quit


def test_review_checks_paths_against_the_snapshot(tmp_path, snapshot_db):  # noqa: F811
    d = queue(tmp_path, candidate("aaaa", "garden question", returned=("GONE/old.md",)))
    out = tmp_path / "real.jsonl"
    said = []
    summary = rv.review(d, out, db=str(snapshot_db), say=said.append,
                        ask=scripted("1", "NOPE/missing.md", "PROJECTS/Garden.md", "", ""))
    assert summary["accepted"] == 1
    assert "not in the snapshot: GONE/old.md" in said and "not in the snapshot: NOPE/missing.md" in said
    assert load_set(out).rows[0]["expect"]["sources"][0]["match"]["path"] == "PROJECTS/Garden.md"


def test_review_refuses_a_public_set(tmp_path):
    d = queue(tmp_path, candidate("aaaa", "q"))
    out = tmp_path / "public.jsonl"
    write_set([{"id": "x", "question": "q", "lang": "en", "kind": "fact", "origin": "synthetic",
                "expect": {"abstain": False, "sources": [{"store": "vault", "match": {"path": "a.md"}}]}}],
              out, privacy="public", fixture="eval/public")
    with pytest.raises(rv.ReviewError):
        rv.review(d, out, ask=scripted("1"), say=lambda *_: None)


def test_review_cli_stats_and_stdin(tmp_path, monkeypatch, capsys):
    d = queue(tmp_path, candidate("aaaa", "secret question one"),
              candidate("bbbb", "secret question two", signal="gate_rejected_pool"))
    assert eval_cli.main(["review", str(d), "--stats"]) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["pending"] == 2 and stats["by_signal"] == {"gate_rejected_pool": 1, "searched_after_empty": 1}
    assert "secret" not in json.dumps(stats)
    assert eval_cli.main(["review", str(d)]) == 2
    monkeypatch.setattr("sys.stdin", io.StringIO("1\n\n\nr\n"))
    out = tmp_path / "real.jsonl"
    assert eval_cli.main(["review", str(d / mc.QUEUE_FILE), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert '"accepted": 1' in printed and '"rejected": 1' in printed
    assert validate(out) == [] and mc.read_queue(d / mc.QUEUE_FILE) == []
