"""Snapshot admission state machine: watcher swaps warm, rejections cost nothing per lease,
retries on change or after the delay, health red states, and a bad pointer at startup."""
from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from quoth import serving, serving_cache, sidecar
from quoth.db import open_db
from quoth.embed.backends import get_embedder
from quoth.embed.store import embed_chunks
from quoth.evidence_service import ReadOnlyEvidence
from quoth.ingest import ingest_vault
from quoth.refresh import sha
from quoth.serving import ServingSnapshot, SnapshotUnavailable, replace_pointer


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _freeze(path):
    path.with_suffix(".manifest.json").write_text(json.dumps({
        "db": str(path.resolve()), "db_sha256": sha(path), "db_bytes": path.stat().st_size,
        "status": "frozen_current_vault_candidate", "integrity": "ok",
        "schema": "validate_evidence_db"}))


@pytest.fixture
def snapshots(vault, db_path, tmp_path):
    conn = open_db(db_path)
    ingest_vault(conn, vault)
    embed_chunks(conn, get_embedder("hash-ngram"), batch_size=8)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()
    second = tmp_path / "second.db"
    with sqlite3.connect(db_path) as source, sqlite3.connect(second) as target:
        source.backup(target)
        target.execute("UPDATE chunks SET text=text || ' successor'")
    source.close()
    target.close()
    for db in (db_path, second):
        _freeze(db)
    pointer = tmp_path / "serving.db"
    pointer.symlink_to(db_path)
    return db_path, second, pointer


@pytest.fixture
def counted(monkeypatch):
    calls = []
    real = serving.open_snapshot

    def counting(target):
        calls.append(target)
        return real(target)

    monkeypatch.setattr(serving, "open_snapshot", counting)
    return calls


def _break_manifest(db):
    manifest = db.with_suffix(".manifest.json")
    manifest.write_text(json.dumps({**json.loads(manifest.read_text()), "db_sha256": "0" * 64}))


def test_watcher_validates_and_warms_before_the_swap(snapshots):
    first, second, pointer = snapshots
    snap = ServingSnapshot(pointer)
    evidence = ReadOnlyEvidence(snap, warm_models=("hash-ngram",))
    seen = []
    prepare = snap.prepare

    def watching(conn):
        # The candidate is warmed while the admitted snapshot still serves.
        with snap.lease() as (_, identity):
            seen.append(identity)
        prepare(conn)

    snap.prepare = watching
    try:
        replace_pointer(pointer, second)
        snap.start_watcher(0.02)
        for _ in range(500):
            if snap.snapshot_id == sha(second):
                break
            threading.Event().wait(0.01)
        assert snap.snapshot_id == sha(second) and seen == [sha(first)]
        with snap.lease() as (conn, _):
            st = serving_cache._STATE[conn]  # warmed state carried over: no cold first turn
            assert "hash-ngram" in st.vectors_ok and st._matrices and st._dates is not None
        out = evidence.bundle("tomatoes basil", 3, "hash-ngram")
        assert out["provenance"]["snapshot_id"] == sha(second)
        assert snap.status()["state"] == "admitted" and snap.health()["status"] == "ok"
    finally:
        snap.close()


def test_a_rejected_pointer_costs_no_validation_per_lease(snapshots, counted):
    first, second, pointer = snapshots
    clock = Clock()
    snap = ServingSnapshot(pointer, clock=clock)
    assert len(counted) == 1  # startup admission
    _break_manifest(second)
    replace_pointer(pointer, second)
    assert snap.poll() == "rejected" and len(counted) == 2
    for _ in range(25):
        with snap.lease() as (_, identity):
            assert identity == sha(first)
    for _ in range(5):
        assert snap.poll() == "rejected"
    assert len(counted) == 2  # remembered: neither leases nor polls revalidate it
    health = snap.health()
    assert health["status"] == "pointer_rejected" and health["state"] == "rejected"
    assert health["snapshot_id"] == sha(first)
    rejected = health["rejected"]
    assert rejected["reason"] == "manifest_invalid" and rejected["attempts"] == 1
    assert rejected["retry_in_s"] == serving.REJECT_RETRY[0]
    assert len(rejected["target_digest"]) == 16 and str(second) not in json.dumps(health)
    snap.close()


def test_rejected_target_is_retried_after_the_delay_then_backs_off(snapshots, counted):
    _, second, pointer = snapshots
    clock = Clock()
    snap = ServingSnapshot(pointer, clock=clock)
    _break_manifest(second)
    replace_pointer(pointer, second)
    assert snap.poll() == "rejected" and len(counted) == 2
    clock.t += serving.REJECT_RETRY[0] - 1
    assert snap.poll() == "rejected" and len(counted) == 2
    clock.t += 2
    assert snap.poll() == "rejected" and len(counted) == 3
    rejected = snap.status()["rejected"]
    assert rejected["attempts"] == 2 and rejected["retry_in_s"] == serving.REJECT_RETRY[1]
    clock.t += serving.REJECT_RETRY[1] + 1
    snap.poll()
    clock.t += serving.REJECT_RETRY[2] + 1
    snap.poll()
    assert snap.status()["rejected"]["retry_in_s"] == serving.REJECT_RETRY[-1]  # capped
    assert snap.poll(force=True) == "rejected" and len(counted) == 6
    snap.close()


def test_rejected_target_is_retried_when_the_file_changes(snapshots, counted):
    _, second, pointer = snapshots
    snap = ServingSnapshot(pointer, clock=Clock())
    good = second.with_suffix(".manifest.json").read_text()
    _break_manifest(second)
    replace_pointer(pointer, second)
    assert snap.poll() == "rejected"
    second.with_suffix(".manifest.json").write_text(good)  # the manifest's identity changes
    assert snap.poll() == "admitted" and len(counted) == 3
    assert snap.snapshot_id == sha(second) and snap.health()["status"] == "ok"
    assert "rejected" not in snap.status()
    snap.close()


def test_pointer_back_to_the_admitted_target_clears_the_rejection(snapshots, counted):
    first, second, pointer = snapshots
    snap = ServingSnapshot(pointer, clock=Clock())
    _break_manifest(second)
    replace_pointer(pointer, second)
    assert snap.poll() == "rejected"
    replace_pointer(pointer, first)
    assert snap.poll() == "admitted" and len(counted) == 2  # nothing to validate
    assert snap.health()["status"] == "ok"
    snap.close()


def test_candidate_pending_over_an_hour_is_red(snapshots):
    _, second, pointer = snapshots
    clock = Clock()
    snap = ServingSnapshot(pointer, clock=clock)
    during = []

    def slow(conn):
        during.append(snap.health())
        clock.t += serving.CANDIDATE_MAX_AGE + 1
        during.append(snap.health())

    snap.prepare = slow
    replace_pointer(pointer, second)
    assert snap.poll() == "admitted"
    assert during[0]["status"] == "ok" and during[0]["state"] == "candidate"
    assert during[1]["status"] == "candidate_stalled"
    assert snap.health()["status"] == "ok"
    snap.close()


def test_a_failed_warmup_rejects_the_candidate(snapshots):
    first, second, pointer = snapshots
    snap = ServingSnapshot(pointer, clock=Clock())

    def broken(conn):
        raise RuntimeError("/private/path in an error")

    snap.prepare = broken
    replace_pointer(pointer, second)
    assert snap.poll() == "rejected" and snap.snapshot_id == sha(first)
    assert snap.status()["rejected"]["reason"] == "warmup_failed"
    snap.close()


def test_a_stalled_watcher_is_red(snapshots):
    _, _, pointer = snapshots
    clock = Clock()
    snap = ServingSnapshot(pointer, clock=clock).start_watcher(1000)
    assert snap.health()["status"] == "ok"
    clock.t += 1000 * serving.WATCHER_STALE_INTERVALS + 1
    health = snap.health()
    assert health["status"] == "watcher_stalled" and health["watcher"]["interval_s"] == 1000
    snap.close()


def test_require_still_refuses_a_bad_pointer(snapshots, tmp_path):
    _, _, pointer = snapshots
    pointer.unlink()
    pointer.symlink_to(tmp_path / "missing.db")
    with pytest.raises(ValueError):
        ServingSnapshot(pointer)


def test_sidecar_starts_without_a_snapshot_and_recovers(snapshots, tmp_path):
    first, _, pointer = snapshots
    pointer.unlink()
    pointer.symlink_to(tmp_path / "missing.db")
    snap = ServingSnapshot(pointer, require=False, clock=Clock())
    app = sidecar.Quoth(ledger_path=tmp_path / "ledger.db",
                        evidence=ReadOnlyEvidence(snap, warm_models=("hash-ngram",)),
                        model="hash-ngram")
    try:
        health = app.health()
        assert health["status"] == "red" and health["evidence"]["status"] == "unavailable"
        assert health["evidence"]["state"] == "unadmitted"
        assert health["evidence"]["rejected"]["reason"] == "target_missing"
        with pytest.raises(SnapshotUnavailable):
            with snap.lease():
                pass
        with pytest.raises(sidecar.SidecarError) as err:
            app.evidence_bundle({"query": "tomatoes basil", "k": 3})
        assert err.value.code == "evidence_unavailable" and err.value.status == 503
        out = app.prefetch({"query": "tomatoes basil", "dry_run": True})
        assert out["detail"]["status"]["vault"] == "unavailable"  # claims-only turn

        replace_pointer(pointer, first)
        assert snap.poll() == "admitted"
        health = app.health()
        assert health["evidence"]["status"] == "ok" and health["evidence"]["snapshot_id"] == sha(first)
        assert health["status"] == "green"
        assert app.evidence_bundle({"query": "tomatoes basil", "k": 3})["hits"]
    finally:
        app.close()
        snap.close()


def test_sidecar_main_does_not_crash_on_a_bad_pointer(snapshots, tmp_path, monkeypatch):
    """The startup path builds the app with no snapshot instead of raising (no crash loop)."""
    _, _, pointer = snapshots
    pointer.unlink()
    pointer.symlink_to(tmp_path / "missing.db")
    built = {}

    class Stop(Exception):
        pass

    def fake_server(path, app, mode=0o660):
        built["app"] = app
        raise Stop

    monkeypatch.setattr(sidecar, "SidecarServer", fake_server)
    with pytest.raises(Stop):
        sidecar.main(["--db", str(pointer), "--ledger", str(tmp_path / "l.db"), "--state-db", "",
                      "--model", "hash-ngram", "--no-warm", "--socket", str(tmp_path / "s.sock")])
    app = built["app"]
    try:
        assert app.health()["evidence"]["status"] == "unavailable"
        assert app.evidence.snapshot.status()["watcher"]["interval_s"] == 5.0
    finally:
        app.close()
        app.evidence.snapshot.close()
