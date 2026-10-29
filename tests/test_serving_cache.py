"""Read-only evidence_bundle caches: byte-identical output, swap invalidation, warmup."""
from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from quoth import serving_cache
from quoth.dated import build_evidence
from quoth.db import connect_read_only, open_db
from quoth.embed.backends import EmbedderUnavailable, get_embedder
from quoth.embed.store import embed_chunks
from quoth.ingest import ingest_vault
from quoth.refresh import sha
from quoth.serving import ServingSnapshot, replace_pointer

pytest.importorskip("mcp")

QUERIES = ["tomatoes basil", "platypus", "медитация по утрам", "vault folders", "platypus",
           "ferritin protocol", "zanzibar", "Quarterly plan pipeline", "tomatoes basil", "  dream  "]


@pytest.fixture
def db(vault, db_path):
    # Duplicate notes give identical vectors, so ties exercise the (-sim, id) tie-break.
    # Same stem + text in different folders: identical passage text, identical vectors.
    for i in range(4):
        (vault / f"DUP{i}").mkdir()
        (vault / f"DUP{i}/2026-02-01 twin.md").write_text(
            "---\ncreated: 2026-02-01\n---\nTomatoes basil twin note.\n", encoding="utf-8")
    conn = open_db(db_path)
    ingest_vault(conn, vault)
    embed_chunks(conn, get_embedder("hash-ngram"), batch_size=8)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()
    return db_path


def _tool(server):
    return server._tool_manager.get_tool("evidence_bundle").fn


def _expected(path, query, k, identity):
    """The pre-cache tool output: default build_evidence path on a fresh connection."""
    conn = connect_read_only(path)
    try:
        bundle = build_evidence(conn, query, k=k, model="hash-ngram",
                                embedder=get_embedder("hash-ngram"))
    finally:
        conn.close()
    bundle["provenance"] = {"snapshot_id": identity}
    for hit in bundle["hits"]:
        hit["provenance"] = {"snapshot_id": identity}
    return json.dumps(bundle, ensure_ascii=False)


def _freeze(path):
    path.with_suffix(".manifest.json").write_text(json.dumps({
        "db": str(path.resolve()), "db_sha256": sha(path), "db_bytes": path.stat().st_size,
        "status": "frozen_current_vault_candidate", "integrity": "ok",
        "schema": "validate_evidence_db"}))


def test_cached_output_is_byte_identical(db):
    from quoth.mcp_server import build_server
    fn = _tool(build_server(str(db)))
    identity = sha(db)
    checked = 0
    for k in (1, 2, 3, 5, 10, 20):
        for q in QUERIES:
            assert fn(query=q, k=k, model="hash-ngram") == _expected(db, q, k, identity)
            checked += 1
    assert checked == 60


def test_ranked_matches_dense_topk_with_ties(db):
    from quoth.dense import dense_topk
    conn = connect_read_only(db)
    try:
        emb = get_embedder("hash-ngram")
        st = serving_cache.state(conn)
        qe = serving_cache.QueryEmbedder()
        for q in QUERIES:
            for k in (1, 3, 4, 7, 50, 10_000):
                assert qe.topk(st, "hash-ngram", emb, q, k) == dense_topk(
                    conn, q, (), k, "hash-ngram", emb)
        sims = qe.topk(st, "hash-ngram", emb, "Tomatoes basil twin note.", 10)
        twins = [cid for cid, s in sims if s == sims[0][1]]
        assert len(twins) >= 4 and twins == sorted(twins)
    finally:
        serving_cache.drop_cache(conn)
        conn.close()


def test_query_lru_is_bounded_and_returns_same_vector(db):
    emb = get_embedder("hash-ngram")
    calls = []

    class Counting:
        spec = emb.spec

        def embed_query(self, text):
            calls.append(text)
            return emb.embed_query(text)

    qe = serving_cache.QueryEmbedder(maxsize=2)
    a = qe.vector("hash-ngram", Counting(), "a")
    assert qe.vector("hash-ngram", Counting(), "a") is a
    qe.vector("hash-ngram", Counting(), "b")
    qe.vector("hash-ngram", Counting(), "c")  # evicts "a"
    qe.vector("hash-ngram", Counting(), "a")
    assert calls == ["a", "b", "c", "a"]
    assert not a.flags.writeable


def test_cache_dropped_on_pointer_swap(db, tmp_path):
    from quoth.mcp_server import build_server
    second = tmp_path / "second.db"
    with sqlite3.connect(db) as source, sqlite3.connect(second) as target:
        source.backup(target)
        target.execute("UPDATE chunks SET text=text || ' successor'")
    source.close()
    target.close()
    for snapshot in (db, second):
        _freeze(snapshot)
    pointer = tmp_path / "serving.db"
    pointer.symlink_to(db)

    snap = ServingSnapshot(pointer)
    with snap.lease() as (old, _):
        serving_cache.state(old).matrix("hash-ngram")
        assert old in serving_cache._STATE
    replace_pointer(pointer, second)
    with snap.lease() as (same, identity):  # leases never swap: the watcher does
        assert same is old and identity == sha(db)
    assert snap.poll() == "admitted"
    with snap.lease() as (new, identity):
        assert identity == sha(second)
        assert old not in serving_cache._STATE
    snap.close()
    assert new not in serving_cache._STATE

    # Through the tool: the watcher swaps, and the answer is the successor's, not a stale cache.
    pointer.unlink()
    pointer.symlink_to(db)
    fn = _tool(build_server(str(pointer), warm_models=("hash-ngram",), watch_interval=0.05))
    assert fn(query="tomatoes basil", k=3, model="hash-ngram") == _expected(
        db, "tomatoes basil", 3, sha(db))
    replace_pointer(pointer, second)
    expected = _expected(second, "tomatoes basil", 3, sha(second))
    for _ in range(200):
        out = fn(query="tomatoes basil", k=3, model="hash-ngram")
        if out == expected:
            break
        threading.Event().wait(0.05)
    assert out == expected and "successor" in out


def test_foreign_commit_resets_plain_read_only_cache(db):
    from quoth.mcp_server import build_server
    fn = _tool(build_server(str(db)))
    before = fn(query="tomatoes basil", k=3, model="hash-ngram")
    with sqlite3.connect(db) as w:
        w.execute("UPDATE chunks SET text=text || ' rewritten'")
    w.close()
    after = fn(query="tomatoes basil", k=3, model="hash-ngram")
    assert after != before and "rewritten" in after
    assert after == _expected(db, "tomatoes basil", 3, sha(db)).replace(
        sha(db), json.loads(after)["provenance"]["snapshot_id"])


def _state_for(path):
    for conn, st in list(serving_cache._STATE.items()):
        try:
            if conn.execute("PRAGMA database_list").fetchone()[2] == str(path.resolve()):
                return st
        except sqlite3.ProgrammingError:
            continue
    raise AssertionError("no cached state")


def _join_warmup():
    for t in threading.enumerate():
        if t.name == "quoth-warmup":
            t.join(30)
            assert not t.is_alive()


def test_warmup_preloads_matrix_and_embedder(db, monkeypatch):
    from quoth.mcp_server import build_server
    loads = []
    real = serving_cache.get_embedder
    monkeypatch.setattr(serving_cache, "get_embedder", lambda m, **kw: loads.append(m) or real(m, **kw))
    server = build_server(str(db), warm_models=("hash-ngram",))
    _join_warmup()
    assert loads == ["hash-ngram"]
    st = _state_for(db)
    assert "hash-ngram" in st.vectors_ok and st._matrices and st._dates is not None
    fn = _tool(server)
    assert fn(query="tomatoes basil", k=5, model="hash-ngram") == _expected(
        db, "tomatoes basil", 5, sha(db))
    assert loads == ["hash-ngram"]


def test_warmup_failure_keeps_fail_closed(db, monkeypatch):
    from quoth.mcp_server import build_server

    def unavailable(model, **kw):
        raise EmbedderUnavailable("no model files")

    monkeypatch.setattr(serving_cache, "get_embedder", unavailable)
    # bge-m3 has no vectors here and hash-ngram cannot load: both warmups fail quietly.
    server = build_server(str(db), warm_models=("bge-m3", "hash-ngram"))
    _join_warmup()
    fn = _tool(server)
    with pytest.raises(EmbedderUnavailable):
        fn(query="tomatoes basil", k=5, model="hash-ngram")
    with pytest.raises(RuntimeError, match="no current vectors for bge-m3"):
        fn(query="tomatoes basil", k=5, model="bge-m3")
    with pytest.raises(EmbedderUnavailable):  # failures are never cached as success
        fn(query="tomatoes basil", k=5, model="hash-ngram")
    monkeypatch.setattr(serving_cache, "get_embedder", get_embedder)
    assert fn(query="tomatoes basil", k=5, model="hash-ngram") == _expected(
        db, "tomatoes basil", 5, sha(db))
