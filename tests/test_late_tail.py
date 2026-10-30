"""Record-only late-tail measurement: the prefetch log's per-job events and `eval online`'s
window report, on isolated synthetic services (no live ledger, snapshot or model).

Checks, by the measurement plan: (1) on-time and late jobs, both orderings of the turn's
return and the job's done callback, one completion per job, exceptions and shutdown
censoring; (2) raw nonempty but post-gate empty, admissible nonempty, a snapshot swap during
the job, no extra rerank or serving write; (3) busy turns start no job, logging never blocks,
dropped and failed lines are counted; (4) the answer, order, gates and budget are the same
with the log on and off, with a bounded overhead; (5) provenance is complete and neither
query nor excerpt text reaches the encoded bytes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import threading
import time
import tracemalloc

import pytest

from quoth import rerank as rerank_mod
from quoth import sidecar
from quoth.cli import main as cli_main
from quoth.evals.online import online
from quoth.ledger import core
from quoth.prefetch_log import PrefetchLog
from quoth.relevance import Gate

QUERY = "where did the quarterly zebrafish inventory land"
EXCERPTS = ("aquamarine-excerpt-one", "aquamarine-excerpt-two", "aquamarine-excerpt-three")


class _Pointer:
    """A serving pointer the test can swap while a job runs."""

    def __init__(self, snapshot_id="snap-A"):
        self.snapshot_id = snapshot_id

    def health(self):
        return {"status": "ok", "snapshot_id": self.snapshot_id}


class _Evidence:
    """Synthetic vault: fixed hits, a controllable delay or gate, optional failure."""

    retrieval = "hybrid"

    def __init__(self, cosines=(0.8, 0.75, 0.7), delay=0.0, gate=None, fail=None, swap_to=None):
        self.queries = object()  # the sidecar's prefetch path (with scores), no claim index use
        self.snapshot = _Pointer()
        self.cosines, self.delay, self.gate, self.fail, self.swap_to = cosines, delay, gate, fail, swap_to
        self.calls = 0
        self.started = threading.Event()

    def bundle(self, query, k, model, **_):
        self.calls += 1
        searched = self.snapshot.snapshot_id  # the lease this search holds
        self.started.set()
        if self.swap_to is not None:
            self.snapshot.snapshot_id = self.swap_to  # the pointer moves on mid-job
        if self.gate is not None:
            self.gate.wait(10)
        if self.delay:
            time.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        hits = [{"locator": f"N{i}.md:1-4", "path": f"N{i}.md", "text": EXCERPTS[i % 3] + " zebrafish",
                 "cosine": c, "named": False, "note_date": None} for i, c in enumerate(self.cosines)]
        return {"hits": hits, "provenance": {"snapshot_id": searched}}


def _app(tmp_path, evidence, *, log=True, budget=0.2, gate=None, reranker=None, max_queue=2000, name="log"):
    plog = PrefetchLog(tmp_path / name, max_queue=max_queue) if log else None
    app = sidecar.Quoth(ledger_path=tmp_path / f"{name}.db", evidence=evidence, evidence_budget=budget,
                        prefetch_log=plog, gate=gate, reranker=reranker)
    return app, plog


def _records(tmp_path, name="log"):
    out = []
    for f in sorted((tmp_path / name).glob("*.jsonl")):
        out += [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()]
    return out


def _wait_for(plog, tmp_path, pred, name="log", tries=200):
    for _ in range(tries):
        plog.flush()
        recs = _records(tmp_path, name)
        if pred(recs):
            return recs
        time.sleep(0.01)
    raise AssertionError("expected records never appeared")


def _of(recs, kind):
    return [r for r in recs if r["type"] == kind]


def _has_done(recs):
    return bool(_of(recs, "done"))


# ------------------------------------------------------------------ 1. events and completion

def test_on_time_job_writes_every_event_and_one_completion(tmp_path):
    ev = _Evidence()
    app, plog = _app(tmp_path, ev, budget=2.0)
    try:
        out = app.prefetch({"query": QUERY, "session_id": "s1"})
        assert out["evidence_status"] == "ok" and out["evidence"] == 3
        recs = _wait_for(plog, tmp_path, _has_done)
    finally:
        app.close()
    recs = _records(tmp_path)
    (start,), (sub,), (wait,), (ret,), (done,) = (_of(recs, k) for k in
                                                  ("start", "submitted", "wait", "prefetch", "done"))
    req, job = start["req"], sub["job"]
    assert sub["req"] == wait["req"] == ret["req"] == done["req"] == req
    assert wait["job"] == ret["job"] == done["job"] == job and req != job
    assert len({r["inst"] for r in recs}) == 1  # one process instance on every line
    assert sub["submitted_ms"] <= wait["wait_ms"] <= ret["wait_end_ms"] <= ret["returned_ms"] < wait["deadline_ms"]
    assert wait["budget_ms"] == 2000.0 and wait["deadline_ms"] == pytest.approx(wait["wait_ms"] + 2000, abs=0.02)
    assert done["outcome"] == "normal" and done["snapshot_id"] == "snap-A" and done["raw_n"] == 3
    assert done["completed_ms"] <= wait["deadline_ms"]
    # On time, the counterfactual is the turn's own decision.
    injected = [v["locator"] for v in ret["stores"]["vault"] if v["injected"]]
    assert done["decision"] == "computed" and done["would_inject"] == injected
    (close,) = [r for r in _of(recs, "counters") if r["reason"] == "close"]
    assert close["jobs_started"] == close["jobs_completed"] == 1
    assert close["in_flight"] == close["unjoined_completions"] == close["dropped"] == 0


def test_late_job_completes_after_the_turn_returned(tmp_path):
    release = threading.Event()
    app, plog = _app(tmp_path, _Evidence(gate=release), budget=0.05)
    try:
        out = app.prefetch({"query": QUERY, "session_id": "s1"})
        assert out["evidence_status"] == "slow" and out["evidence"] == 0
        plog.flush()
        assert not _of(_records(tmp_path), "done")  # still running
        assert plog.counters()["in_flight"] == 1
        time.sleep(0.15)
        release.set()
        recs = _wait_for(plog, tmp_path, _has_done)
    finally:
        app.close()
    (wait,), (ret,), (done,) = _of(recs, "wait"), _of(recs, "prefetch"), _of(recs, "done")
    assert ret["status"]["vault"] == "slow" and ret["wait_end_ms"] >= wait["deadline_ms"] - 1
    assert done["outcome"] == "normal" and done["completed_ms"] > wait["deadline_ms"] + 100
    assert done["would_inject_n"] == 3 and done["decision"] == "computed"
    assert recs.index(ret) < recs.index(done)  # this ordering: return, then completion


def test_completion_before_return_joins_by_id(tmp_path, monkeypatch):
    """The other ordering: the done line is written before the turn's prefetch line."""
    app, plog = _app(tmp_path, _Evidence(), budget=2.0)
    real = app._log_prefetch

    def late_return(*args, **kwargs):
        for _ in range(200):  # the done line is on disk before the return line is queued
            plog.flush()
            if _of(_records(tmp_path), "done"):
                break
            time.sleep(0.005)
        real(*args, **kwargs)

    monkeypatch.setattr(app, "_log_prefetch", late_return)
    try:
        app.prefetch({"query": QUERY, "session_id": "s1"})
        recs = _wait_for(plog, tmp_path, lambda r: _of(r, "prefetch"))
        assert plog.counters()["completed_before_return"] == 1
    finally:
        app.close()
    recs = _records(tmp_path)
    (ret,), (done,) = _of(recs, "prefetch"), _of(recs, "done")
    assert recs.index(done) < recs.index(ret) and done["req"] == ret["req"]
    report = online(tmp_path / "log")["late_tail"]
    assert report["turns"] == 1 and report["status"] == {"ok": 1}
    assert report["jobs"]["outcomes"] == {"normal": 1} and report["missing"] == {"return": 0, "wait": 0, "done": 0}


def test_each_job_has_exactly_one_completion(tmp_path):
    ev = _Evidence(delay=0.01)
    app, plog = _app(tmp_path, ev, budget=1.0)
    try:
        for i in range(12):
            app.prefetch({"query": f"{QUERY} {i}", "session_id": "s"})
            app.wait_idle()
        _wait_for(plog, tmp_path, lambda r: len(_of(r, "done")) == 12)
    finally:
        app.close()
    recs = _records(tmp_path)
    jobs = [r["job"] for r in _of(recs, "submitted")]
    dones = [r["job"] for r in _of(recs, "done")]
    assert len(jobs) == 12 == ev.calls and sorted(jobs) == sorted(dones) and len(set(dones)) == 12


@pytest.mark.parametrize("budget,status", [(1.0, "unavailable"), (0.05, "slow")])
def test_failed_job_records_the_exception_type_only(tmp_path, budget, status):
    ev = _Evidence(delay=0.0 if status == "unavailable" else 0.2,
                   fail=OSError("cannot open /home/someone/private/vault.db"))
    app, plog = _app(tmp_path, ev, budget=budget)
    try:
        assert app.prefetch({"query": QUERY, "session_id": "s1"})["evidence_status"] == status
        _wait_for(plog, tmp_path, _has_done)
    finally:
        app.close()
    raw = b"".join(f.read_bytes() for f in (tmp_path / "log").glob("*.jsonl"))
    (done,) = _of(_records(tmp_path), "done")
    assert done["outcome"] == "exception" and done["exc_type"] == "OSError"
    assert b"private" not in raw and b"/home" not in raw and b"cannot open" not in raw


def test_shutdown_censors_unresolved_jobs_once(tmp_path):
    release = threading.Event()
    app, plog = _app(tmp_path, _Evidence(gate=release), budget=0.05)
    assert app.prefetch({"query": QUERY, "session_id": "s1"})["evidence_status"] == "slow"
    app.close()
    release.set()  # the job finishes after the close: no second completion
    time.sleep(0.1)
    recs = _records(tmp_path)
    (done,) = _of(recs, "done")
    assert done["outcome"] == "censored" and done["censored_reason"] == "shutdown"
    assert done["running_ms"] >= 50
    (close,) = [r for r in _of(recs, "counters") if r["reason"] == "close"]
    assert close["jobs_censored"] == 1 and close["jobs_completed"] == 0 and close["in_flight"] == 0
    report = online(tmp_path / "log")["late_tail"]
    assert report["jobs"]["outcomes"] == {"censored": 1}
    assert report["budgets"]["by_budget"]["2.8s"]["censored"] == 1


# ------------------------------------------------------------------ 2. counterfactual and provenance

def test_raw_nonempty_but_post_gate_empty(tmp_path):
    release = threading.Event()
    app, plog = _app(tmp_path, _Evidence(cosines=(0.31, 0.3, 0.29), gate=release), budget=0.05)
    try:
        app.prefetch({"query": QUERY, "session_id": "s1"})
        time.sleep(0.1)
        release.set()
        recs = _wait_for(plog, tmp_path, _has_done)
    finally:
        app.close()
    (done,) = _of(recs, "done")
    assert done["raw_n"] == 3 and len(done["candidates"]) == 3
    assert done["decision"] == "computed" and done["would_inject"] == [] and done["would_inject_n"] == 0
    by = online(tmp_path / "log")["late_tail"]["budgets"]["by_budget"]
    assert by["2.8s"]["empty_post_gate"] == 1 and by["2.8s"]["admissible_nonempty"] == 0


def test_admissible_nonempty_late_job_and_budget_replay(tmp_path):
    release = threading.Event()
    app, plog = _app(tmp_path, _Evidence(cosines=(0.8, 0.6), gate=release), budget=0.05)
    try:
        app.prefetch({"query": QUERY, "session_id": "s1"})
        time.sleep(0.3)
        release.set()
        recs = _wait_for(plog, tmp_path, _has_done)
    finally:
        app.close()
    (done,) = _of(recs, "done")
    assert done["would_inject"] == ["N0.md:1-4"]  # 0.6 is more than the 0.15 gap below 0.8
    late = online(tmp_path / "log")["late_tail"]
    assert late["slow"]["n"] == 1 and late["slow"]["completion_minus_deadline_ms"]["n"] == 1
    for label in ("1.5s", "2s", "2.5s", "2.8s"):
        b = late["budgets"]["by_budget"][label]
        assert b["fit"] == b["admissible_nonempty"] == b["admissible_nonempty_within_client_deadline"] == 1


def test_record_keeps_the_snapshot_the_job_searched(tmp_path):
    release = threading.Event()
    ev = _Evidence(gate=release, swap_to="snap-B")
    app, plog = _app(tmp_path, ev, budget=0.05)
    try:
        app.prefetch({"query": QUERY, "session_id": "s1"})
        assert ev.started.wait(2) and app.health()["evidence"]["snapshot_id"] == "snap-B"
        release.set()
        recs = _wait_for(plog, tmp_path, _has_done)
    finally:
        app.close()
    (done,) = _of(recs, "done")
    assert done["snapshot_id"] == "snap-A"
    (stratum,) = online(tmp_path / "log")["late_tail"]["strata"]
    assert stratum["snapshot_id"] == "snap-A" and stratum["slow"] == 1


class _StubReranker:
    budget, claims, name, state = 0.5, False, "stub-rerank", "ready"

    def model(self):
        return object()

    def status(self):
        return {"state": self.state}

    def stop(self):
        pass


def test_late_job_never_reranks_and_is_unknown_when_the_turn_would(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(rerank_mod, "score_pairs",
                        lambda model, q, texts, until, cancel: calls.append(len(texts)) or
                        ([0.9] * len(texts), [1.0] * len(texts)))
    release = threading.Event()
    ev = _Evidence(gate=release)
    app, plog = _app(tmp_path, ev, budget=0.05, gate=Gate(vault_min_rerank=0.5), reranker=_StubReranker())
    try:
        assert app.prefetch({"query": QUERY, "session_id": "s1"})["evidence_status"] == "slow"
        release.set()
        recs = _wait_for(plog, tmp_path, _has_done)
        app.wait_idle()
    finally:
        app.close()
    (start,), (done,) = _of(recs, "start"), _of(recs, "done")
    assert calls == [] and ev.calls == 1  # no rerank, no second search for the log
    assert start["config"]["rerank"]["vault_rules"] is True
    assert done["decision"] == "unknown" and done["would_inject"] is None and done["raw_n"] == 3
    by = online(tmp_path / "log")["late_tail"]["budgets"]["by_budget"]
    assert by["2.8s"]["unknown"] == 1 and by["2.8s"]["admissible_nonempty"] == 0


def test_the_log_writes_nothing_to_serving_or_the_ledger(tmp_path):
    changes = {}
    for log in (False, True):
        ev = _Evidence(delay=0.01)
        app, _ = _app(tmp_path, ev, log=log, budget=1.0, name=f"w{log}")
        try:
            with app.lock:
                core.seed(app.conn, [{"text": "The zebrafish inventory landed in the north lab."}])
            before = app.conn.total_changes
            for i in range(3):
                app.prefetch({"query": f"{QUERY} {i}", "session_id": "s"})
                app.wait_idle()
            changes[log] = (app.conn.total_changes - before, ev.calls)
        finally:
            app.close()
    assert changes[True] == changes[False] and changes[True][1] == 3


def test_start_record_carries_provenance(tmp_path):
    app, plog = _app(tmp_path, _Evidence(), budget=1.0)
    try:
        app.prefetch({"query": QUERY, "session_id": "s1"})
        recs = _wait_for(plog, tmp_path, _has_done)
    finally:
        app.close()
    (inst,), (start,) = _of(recs, "instance"), _of(recs, "start")
    assert inst["code_sha256"] == start["code_sha256"] and inst["pid"] > 0 and inst["v"] == start["v"]
    cfg = start["config"]
    assert cfg["gate"] == Gate(**sidecar.D.GATE).to_dict() and cfg["retrieval"] == "hybrid"
    assert cfg["k_evidence"] == sidecar.D.PREFETCH_EVIDENCE and cfg["vault_pool"] == sidecar.D.VAULT_POOL
    assert cfg["evidence_budget_ms"] == 1000.0 and cfg["rerank"] is None
    assert len(start["config_sha256"]) == 16 and len(start["query_sha256"]) == 64
    ret = _of(recs, "prefetch")[0]
    assert ret["config_sha256"] == start["config_sha256"]


# ------------------------------------------------------------------ 3. busy, blocking, losses

def test_busy_turn_starts_no_job_and_links_the_running_one(tmp_path):
    release = threading.Event()
    ev = _Evidence(gate=release)
    app, plog = _app(tmp_path, ev, budget=0.05)
    try:
        assert app.prefetch({"query": QUERY, "session_id": "s1"})["evidence_status"] == "slow"
        assert app.prefetch({"query": QUERY + " again", "session_id": "s1"})["evidence_status"] == "busy"
        assert plog.counters()["jobs_started"] == 1 and ev.calls == 1
        release.set()
        recs = _wait_for(plog, tmp_path, _has_done)
    finally:
        app.close()
    (sub,), (busy,) = _of(recs, "submitted"), _of(recs, "busy")
    assert busy["busy_job"] == sub["job"] and busy["job"] is None
    assert len(_of(recs, "done")) == 1 and len(_of(recs, "wait")) == 1
    late = online(tmp_path / "log")["late_tail"]
    assert late["status"] == {"slow": 1, "busy": 1} and late["jobs"]["started"] == 1
    assert late["busy"] == {"n": 1, "linked_to_job": 1, "linked_job_in_window": 1}


def test_logging_never_blocks_and_counts_dropped_lines(tmp_path):
    stuck = threading.Event()
    app, plog = _app(tmp_path, _Evidence(), budget=1.0, max_queue=3)
    real_emit = plog._emit
    plog._emit = lambda item: stuck.wait(10) and real_emit(item)  # the writer thread hangs
    try:
        plog.write({"type": "skip"})  # the writer takes this one and hangs on it
        times = []
        for i in range(20):
            t0 = time.monotonic()
            assert app.prefetch({"query": f"{QUERY} {i}", "session_id": "s"})["evidence_status"] == "ok"
            times.append(time.monotonic() - t0)
            app.wait_idle()
        assert max(times) < 0.5 and plog.dropped > 20
    finally:
        stuck.set()
        app.close()
    dropped = plog.dropped
    (close,) = [r for r in _of(_records(tmp_path), "counters") if r["reason"] == "close"]
    assert close["dropped"] == dropped
    logger = online(tmp_path / "log")["late_tail"]["logger"]
    assert logger["dropped"] == dropped and logger["instances_closed"] == 1


def test_write_errors_are_counted_and_reported(tmp_path):
    plog = PrefetchLog(tmp_path / "log")
    plog.write({"bad": object()})  # not serialisable
    plog.write(lambda: 1 / 0)      # a builder that fails
    plog.flush()
    assert plog.errors == 2
    plog.close()
    (close,) = [r for r in _of(_records(tmp_path), "counters") if r["reason"] == "close"]
    assert close["errors"] == 2 and online(tmp_path / "log")["late_tail"]["logger"]["errors"] == 2


def test_counters_are_written_periodically(tmp_path):
    plog = PrefetchLog(tmp_path / "log", counters_every=0.05)
    time.sleep(0.3)
    plog.close()
    reasons = [r["reason"] for r in _of(_records(tmp_path), "counters")]
    assert reasons.count("periodic") >= 2 and reasons[-1] == "close"


# ------------------------------------------------------------------ 4. unchanged behaviour, overhead

@pytest.mark.parametrize("delay,budget", [(0.0, 1.0), (0.3, 0.05)])
def test_answers_are_the_same_with_the_log_on_and_off(tmp_path, delay, budget):
    outs = {}
    for log in (False, True):
        app, _ = _app(tmp_path, _Evidence(cosines=(0.8, 0.7, 0.6, 0.55), delay=delay), log=log,
                      budget=budget, name=f"a{log}")
        try:
            with app.lock:
                core.seed(app.conn, [{"text": "The zebrafish inventory landed in the north lab."},
                                     {"text": "Quarterly counts happen on Mondays."}])
            got = []
            for q in (QUERY, "quarterly zebrafish counts", QUERY):
                got.append(app.prefetch({"query": q, "session_id": "s"}))
                app.wait_idle()
            with app.lock:
                uses = [tuple(r) for r in app.conn.execute("SELECT id, use_count FROM claims ORDER BY id")]
            outs[log] = (got, uses)
        finally:
            app.close()
    assert outs[True] == outs[False]
    assert {o["evidence_status"] for o in outs[True][0]} == {"ok" if delay == 0 else "slow"}


def test_overhead_of_the_log_is_bounded(tmp_path):
    """Wall time and CPU per turn with the log on vs off, on a synthetic fast vault; and the
    job tracking holds nothing once the jobs are done (bounded memory)."""
    n = 150
    result = {}
    for log in (False, True):
        app, plog = _app(tmp_path, _Evidence(), log=log, budget=1.0, name=f"o{log}")
        try:
            for i in range(10):  # warm-up
                app.prefetch({"query": f"warm {i} zebrafish", "session_id": "s"})
                app.wait_idle()
            wall, cpu0 = 0.0, time.process_time()
            for i in range(n):
                t0 = time.perf_counter()
                app.prefetch({"query": f"{QUERY} {i}", "session_id": "s"})
                wall += time.perf_counter() - t0
                app.wait_idle()
            if log:
                plog.flush()
            result[log] = (wall / n * 1000, (time.process_time() - cpu0) / n * 1000)
            if log:  # memory: a second bounded run under tracemalloc (it slows allocation)
                tracemalloc.start()
                for i in range(n):
                    app.prefetch({"query": f"{QUERY} m{i}", "session_id": "s"})
                    app.wait_idle()
                plog.flush()
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                assert plog.counters()["in_flight"] == 0 and not plog._jobs
                result["peak_kib"] = peak / 1024
        finally:
            app.close()
    extra_wall = result[True][0] - result[False][0]
    extra_cpu = result[True][1] - result[False][1]
    print(f"\nlate-tail log overhead per turn: wall {result[False][0]:.3f} -> {result[True][0]:.3f} ms "
          f"(+{extra_wall:.3f}), cpu {result[False][1]:.3f} -> {result[True][1]:.3f} ms (+{extra_cpu:.3f}), "
          f"tracemalloc peak {result['peak_kib']:.0f} KiB over {n} turns")
    assert extra_wall < 3.0 and extra_cpu < 3.0 and result["peak_kib"] < 4096  # ms per turn, KiB


# ------------------------------------------------------------------ 5. privacy of the encoded bytes

def test_no_query_or_excerpt_text_in_the_encoded_log(tmp_path):
    queries = [QUERY, "где лежит квартальная инвентаризация данио", "zebrafish → inventory “quoted”"]
    release = threading.Event()
    ev = _Evidence(gate=release)
    app, plog = _app(tmp_path, ev, budget=0.05)
    try:
        app.prefetch({"query": queries[0], "session_id": "chat-123"})  # slow
        app.prefetch({"query": queries[1], "session_id": "chat-123"})  # busy
        release.set()
        app.wait_idle()
        ev.gate = None
        app.evidence_budget = 1.0
        app.prefetch({"query": queries[2], "session_id": "chat-123"})  # on time
        _wait_for(plog, tmp_path, lambda r: len(_of(r, "done")) == 2)
    finally:
        app.close()
    raw = b"".join(f.read_bytes() for f in (tmp_path / "log").glob("*.jsonl"))
    texts = raw.decode("utf-8")
    # Every word of 4+ characters of each query, and every excerpt, in raw and escaped form.
    needles = {w for q in queries for w in q.split() if len(w) >= 4} | set(EXCERPTS) | {"chat-123"}
    for needle in needles:
        for form in (needle, json.dumps(needle)[1:-1], json.dumps(needle, ensure_ascii=False)[1:-1]):
            assert form.encode("utf-8") not in raw, needle
    assert "zebrafish" not in texts.lower()
    kinds = {r["type"] for r in _records(tmp_path)}
    assert {"instance", "start", "submitted", "busy", "wait", "prefetch", "done", "counters"} <= kinds


# ------------------------------------------------------------------ window report (synthetic lines)

def _turn(req, t, status, *, session="s", job=None, done_at=None, done=None, wait_ms=5.0, budget_ms=1500.0,
          returned_ms=None, busy_job=None):
    ts = lambda dt: dt.isoformat(timespec="milliseconds")  # noqa: E731
    lines = [{"type": "start", "ts": ts(t), "req": req, "inst": "i1", "v": "0.17.0", "code_sha256": "c",
              "config_sha256": "cfg", "session": session}]
    if busy_job is not None:
        lines.append({"type": "busy", "ts": ts(t), "req": req, "job": None, "busy_job": busy_job, "inst": "i1"})
    if job:
        lines.append({"type": "submitted", "ts": ts(t), "req": req, "job": job, "submitted_ms": 1.0, "inst": "i1"})
        lines.append({"type": "wait", "ts": ts(t), "req": req, "job": job, "wait_ms": wait_ms,
                      "budget_ms": budget_ms, "deadline_ms": wait_ms + budget_ms, "inst": "i1"})
    ret = returned_ms if returned_ms is not None else (wait_ms + budget_ms + 3 if status == "slow" else 50.0)
    lines.append({"type": "prefetch", "ts": ts(t + timedelta(milliseconds=ret)), "req": req, "job": job,
                  "session": session, "status": {"vault": status}, "returned_ms": ret,
                  "wait_end_ms": wait_ms + (budget_ms if status == "slow" else 40), "busy_job": busy_job,
                  "stores": {"vault": []}, "ms": {"total": ret}, "inst": "i1"})
    if done is not None:
        lines.append({"type": "done", "ts": ts(done_at or t), "req": req, "job": job, "inst": "i1", **done})
    return lines


def _write(tmp_path, lines):
    day = datetime.now(tz=timezone.utc).date().isoformat()
    d = tmp_path / "w"
    d.mkdir(exist_ok=True)
    (d / f"prefetch-{day}.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    return d


def test_window_report_reconciles_jobs_finishing_after_the_window(tmp_path):
    t0 = datetime.now(tz=timezone.utc).replace(microsecond=0) - timedelta(hours=1)
    good = {"outcome": "normal", "snapshot_id": "snap-A", "raw_n": 3, "decision": "computed",
            "would_inject": ["a.md:1-2"], "would_inject_n": 1, "candidates": ["a.md:1-2"]}
    lines = (_turn("r1", t0, "ok", job="j1", done={**good, "completed_ms": 30.0})
             + _turn("r2", t0 + timedelta(minutes=1), "slow", job="j2",
                     done={**good, "completed_ms": 2205.0}, done_at=t0 + timedelta(minutes=30))
             + _turn("r3", t0 + timedelta(minutes=2), "busy", busy_job="j2")
             + _turn("r4", t0 + timedelta(minutes=3), "slow", job="j4")  # never finished
             + _turn("r5", t0 + timedelta(minutes=4), "slow", job="j5",
                     done={**good, "completed_ms": 2900.0, "would_inject": [], "would_inject_n": 0})
             + _turn("r6", t0 + timedelta(minutes=20), "ok", job="j6", done={**good, "completed_ms": 20.0}))
    lines.append({"type": "tool", "ts": (t0 + timedelta(minutes=1, seconds=5)).isoformat(), "session": "s",
                  "tool": "evidence_bundle", "results": ["a.md:7-9"], "inst": "i1"})
    lines.append({"type": "counters", "ts": t0.isoformat(), "reason": "periodic", "inst": "i1", "dropped": 2,
                  "errors": 0, "in_flight": 1, "unjoined_completions": 0})
    d = _write(tmp_path, lines)
    until = (t0 + timedelta(minutes=10)).isoformat()
    out = online(d, since=t0.isoformat(), until=until)
    late = out["late_tail"]
    assert late["turns"] == 5 and late["status"] == {"ok": 1, "slow": 3, "busy": 1}
    assert late["jobs"]["started"] == 4 and late["jobs"]["completed_after_window"] == 1
    assert late["missing"]["done"] == 1 and late["logger"]["dropped"] == 2
    assert late["logger"]["instances_closed"] == 0  # no close line: the counters may be stale
    assert late["busy"] == {"n": 1, "linked_to_job": 1, "linked_job_in_window": 1}
    by = late["budgets"]["by_budget"]
    # r2 finished 2.2 s after its wait started; r5 2.895 s (empty); r4 is missing.
    assert by["2s"]["not_fit"] == 2 and by["2s"]["missing"] == 1
    assert by["2.5s"]["admissible_nonempty"] == 1 and by["2.5s"]["not_fit"] == 1
    assert by["2.8s"]["admissible_nonempty"] == 1 and by["2.8s"]["not_fit"] == 1
    assert late["budgets"]["denominator_slow_turns"] == 3
    assert late["slow"]["bins_ms"]["500-1000"] == 1 and late["slow"]["bins_ms"]["1300-1500"] == 1
    assert late["explicit_overlap"]["slow_would_inject"] == {"turns": 1, "followed_by_search": 1, "overlap": 1}
    assert "not answer quality" in late["explicit_overlap"]["caption"]
    assert late["durations_ms"]["request"]["n"] == 5 and late["strata"][0]["config_sha256"] == "cfg"
    assert out["turns"] == 5  # the aggregate covers the window's turns, not r6
    # --max-turns: the first N authored turns from --since.
    first = online(d, since=t0.isoformat(), max_turns=2)["late_tail"]
    assert first["turns"] == 2 and first["window"]["truncated"] is True
    assert first["jobs"]["completed_after_window"] == 1  # r2 finished after the 2nd turn's start
    assert cli_main(["eval", "online", "--log", str(d), "--since", t0.isoformat(), "--max-turns", "3"]) == 0
    assert cli_main(["eval", "online", "--log", str(d), "--since", "not-a-time"]) == 2
