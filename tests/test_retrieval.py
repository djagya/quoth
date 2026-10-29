"""0.17 serving: hybrid retrieval, filters, per-note diversity, links, note fetch, per-hit
freshness and recall tiers, on a hash-ngram snapshot carrying the 0.17 index contract
(links table, indexed mtime/size, manifest built_at), created here when the index code does
not yet write it."""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time

import numpy as np
import pytest

from quoth import recall_defaults as D
from quoth import relevance, retrieval, scopes, serving_cache, sidecar, snapshot_contract
from quoth.db import open_db
from quoth.embed.backends import get_embedder
from quoth.embed.store import embed_chunks
from quoth.evidence_service import NoteNotFound, ReadOnlyEvidence
from quoth.ingest import ingest_vault
from quoth.live import LiveCheck
from quoth.refresh import sha
from quoth.relevance import Gate
from quoth.serving import ServingSnapshot

from conftest import NOTES, write_vault

BUILT_AT = "2026-10-28T03:12:44Z"
EXTRA = {
    "PROJECTS/Voice/Voice guide.md": "---\naliases: [Tone of voice]\n---\n# Voice guide\n\n"
                                     "Short sentences. Plain verbs. No exclamation marks.\n",
    "PROJECTS/Voice/Overview.md": "# Voice project\n\nThe project follows [[Voice guide#Rules]] and the"
                                  " [[Secret plan]].\n\n## Scope\n\nPodcast scripts and newsletters.\n",
    "PROJECTS/Atlas/Plan.md": "# Atlas\n\nAtlas evaluation costs a fixed budget per quarter.\n",
    "WORK/Atlas/Plan.md": "# Atlas\n\nAtlas data stays on the work laptop; privacy boundary.\n",
    "COPY/Garden copy.md": "# Garden\n\nTomatoes and basil grow well in the south bed.\n",
    "PROJECTS/Long.md": "# Long\n\n" + "\n\n".join(f"## Part {i}\n\nTomatoes paragraph {i} about basil beds."
                                                 for i in range(6)) + "\n",
}


def _add_contract(db_path: Path, vault: Path) -> None:
    """Swap the indexed links of one chunk for two known ones: resolved and excluded."""
    import sqlite3
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM links")
    (cid,) = conn.execute("SELECT c.id FROM chunks c JOIN source_versions v ON v.id=c.source_version_id"
                          " JOIN sources s ON s.id=v.source_id WHERE s.path='PROJECTS/Voice/Overview.md'"
                          " AND v.is_current=1 ORDER BY c.line_start LIMIT 1").fetchone()
    conn.executemany("INSERT INTO links(chunk_id, target, target_path, heading, line, kind, status)"
                     " VALUES (?,?,?,?,?,?,?)",
                     [(cid, "Voice guide#Rules", "PROJECTS/Voice/Voice guide.md", "Rules", 3, "wikilink", "resolved"),
                      (cid, "Secret plan", None, None, 3, "wikilink", "excluded")])
    conn.commit()
    conn.close()


@pytest.fixture
def vault(tmp_path):
    return write_vault(tmp_path / "vault", {**NOTES, **EXTRA})


@pytest.fixture
def snap(vault, tmp_path):
    db_path = tmp_path / "snap.db"
    conn = open_db(db_path)
    ingest_vault(conn, vault)
    embed_chunks(conn, get_embedder("hash-ngram"), batch_size=8)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()
    _add_contract(db_path, vault)
    db_path.with_suffix(".manifest.json").write_text(json.dumps({
        "db": str(db_path.resolve()), "db_sha256": sha(db_path), "db_bytes": db_path.stat().st_size,
        "status": "frozen_current_vault_candidate", "integrity": "ok", "schema": "validate_evidence_db",
        "built_at": BUILT_AT}))
    return db_path


@pytest.fixture
def evidence(snap, vault):
    ev = ReadOnlyEvidence(ServingSnapshot(str(snap)), vault=vault)
    yield ev
    ev.snapshot.close()


def _paths(bundle):
    return [h["path"] for h in bundle["hits"]]


# ------------------------------------------------------------------ hybrid retrieval

def test_hybrid_keeps_each_hits_real_cosine(evidence):
    out = evidence.bundle("Voice guide exclamation", 10, "hash-ngram", purpose="prefetch", with_scores=True)
    emb = evidence.queries.embedder("hash-ngram")
    q = np.asarray(emb.embed_query("Voice guide exclamation"), dtype=np.float32)
    with evidence.snapshot.lease() as (conn, _):
        st = serving_cache.state(conn)
        ids, mat = st.matrix("hash-ngram")
        cat = st.catalog()
        for hit in out["hits"]:
            cid = next(c for c in cat.by_path[hit["path"]]
                       if conn.execute("SELECT line_start FROM chunks WHERE id=?", (c,)).fetchone()[0]
                       == hit["line_start"])
            assert hit["cosine"] == round(float(mat[int(np.searchsorted(ids, cid))] @ q), 4)
    assert out["provenance"]["retrieval"] == "hybrid"
    assert any("bm25" in h["via"] or "trigram" in h["via"] for h in out["hits"])


def test_a_named_note_ranks_first_and_is_marked_named(evidence):
    out = evidence.bundle("what does the tone of voice note say", 5, "hash-ngram", with_scores=True)
    assert out["hits"][0]["path"] == "PROJECTS/Voice/Voice guide.md"
    assert out["hits"][0]["named"] and "named" in out["hits"][0]["via"]
    by_path = evidence.bundle("PROJECTS/Voice/Voice guide.md", 3, "hash-ngram", with_scores=True)
    assert by_path["hits"][0]["path"] == "PROJECTS/Voice/Voice guide.md" and by_path["hits"][0]["named"]


def test_short_generic_titles_never_name_a_note():
    assert not retrieval.is_name("plan") and not retrieval.is_name("notes")
    assert retrieval.is_name("garden") and retrieval.is_name("voice guide") and retrieval.is_name("2026 01 01")
    names = {"voice guide": frozenset({"V.md"}), "garden": frozenset({"G.md"})}
    assert retrieval.named_paths(names, "Where is the Voice-guide?") == {"V.md"}
    assert retrieval.named_paths(names, "gardener tips") == set()  # whole words only


def test_dense_mode_ranks_by_cosine_alone(snap):
    ev = ReadOnlyEvidence(ServingSnapshot(str(snap)), retrieval="dense")
    try:
        out = ev.bundle("tomatoes basil", 5, "hash-ngram", purpose="prefetch", with_scores=True)
        cosines = [h["cosine"] for h in out["hits"]]
        assert cosines == sorted(cosines, reverse=True) and {h["via"] for h in out["hits"]} == {"dense"}
        assert out["provenance"]["retrieval"] == "dense"
    finally:
        ev.snapshot.close()
    with pytest.raises(ValueError):
        ReadOnlyEvidence(ServingSnapshot(str(snap)), retrieval="lexical")


def test_the_gate_lets_a_named_hit_past_the_floor_only():
    gate = Gate(vault_min_cosine=0.9, vault_max_gap=0.01)
    assert gate.vault(0.2, 0.95, 0.0, named=True) == (True, "name")
    assert gate.vault(0.2, 0.95, 0.0) == (False, "below_floor")
    judged = relevance.judge_vault(gate, frozenset(), [{"text": "x", "cosine": 0.95},
                                                       {"text": "y", "cosine": 0.1, "named": True},
                                                       {"text": "z", "cosine": 0.1}])
    assert [j["gate"] for j in judged] == ["cosine", "name", "below_floor"]


# ------------------------------------------------------------------ filters and projects

def test_path_prefix_is_a_pre_filter(evidence):
    top = evidence.bundle("Atlas privacy boundary", 1, "hash-ngram")
    assert _paths(top) == ["WORK/Atlas/Plan.md"]
    only = evidence.bundle("Atlas privacy boundary", 1, "hash-ngram", path_prefix="PROJECTS/Atlas")
    assert _paths(only) == ["PROJECTS/Atlas/Plan.md"]  # k=1 still fills from inside the prefix
    assert only["hits"][0]["project"] == "PROJECTS"
    root = evidence.bundle("vault conventions folders", 3, "hash-ngram")
    assert {h["project"] for h in root["hits"] if h["path"] == "Vault Conventions.md"} == {""}
    for bad in ("../x", "/abs", "a/../b", "a\\b", "", "./a"):
        with pytest.raises(retrieval.RetrievalInputError):
            evidence.bundle("Atlas", 3, "hash-ngram", path_prefix=bad)


def test_explicit_tier_is_left_out_unless_asked(evidence, monkeypatch):
    monkeypatch.setattr(scopes, "explicit_scopes", lambda: frozenset({"work"}), raising=False)
    query = "Atlas privacy boundary work laptop"
    assert "WORK/Atlas/Plan.md" not in _paths(evidence.bundle(query, 10, "hash-ngram"))
    assert "WORK/Atlas/Plan.md" not in _paths(evidence.bundle(query, 10, "hash-ngram", purpose="prefetch"))
    assert "WORK/Atlas/Plan.md" not in _paths(evidence.bundle(query, 10, "hash-ngram", purpose="prefetch",
                                                              include_explicit=True))
    assert _paths(evidence.bundle(query, 1, "hash-ngram", include_explicit=True)) == ["WORK/Atlas/Plan.md"]
    with pytest.raises(NoteNotFound):
        evidence.bundle(None, 5, "hash-ngram", path="WORK/Atlas/Plan.md")
    assert _paths(evidence.bundle(None, 5, "hash-ngram", path="WORK/Atlas/Plan.md", include_explicit=True))


# ------------------------------------------------------------------ diversity

def test_explicit_bundle_caps_hits_per_note_and_drops_copies(evidence):
    out = evidence.bundle("tomatoes basil beds paragraph", 6, "hash-ngram")
    paths = _paths(out)
    first = paths[:len(set(paths))]
    assert all(first.count(p) <= D.MAX_HITS_PER_NOTE["explicit"] for p in first)
    # The copy at another path has the same excerpt: only one of the two is returned.
    garden = evidence.bundle("tomatoes basil south bed", 10, "hash-ngram")
    assert len({"PROJECTS/Garden.md", "COPY/Garden copy.md"} & set(_paths(garden))) == 1


def test_vault_pick_serves_one_hit_per_note_before_repeats():
    gate = Gate()
    notes = ["a", "a", "b", "a", "c"]
    assert relevance.vault_pick(gate, [True] * 5, 5, 3, notes=notes, per_note=1) == [0, 2, 4]
    assert relevance.vault_pick(gate, [True, True, False, True, False], 5, 3, notes=notes, per_note=1) == [0, 1, 3]
    assert relevance.vault_pick(gate, [True] * 5, 5, 3) == [0, 1, 2]


def test_prefetch_serves_distinct_notes(snap, tmp_path):
    app = sidecar.Quoth(ledger_path=tmp_path / "l.db", evidence=ReadOnlyEvidence(ServingSnapshot(str(snap))),
                        model="hash-ngram", gate=Gate())
    try:
        out = app.prefetch({"query": "tomatoes paragraph about basil beds", "dry_run": True})
        served = [e["locator"].rsplit(":", 1)[0] for e in out["detail"]["evidence"]]
        assert len(served) == 3 and len(set(served)) == 3
    finally:
        app.close()


# ------------------------------------------------------------------ links and note fetch

def test_hits_list_links_and_never_a_path_for_an_excluded_target(evidence):
    out = evidence.bundle(None, 5, "hash-ngram", path="PROJECTS/Voice/Overview.md")
    links = out["hits"][0]["links"]
    assert links[0] == {"path": "PROJECTS/Voice/Voice guide.md", "heading": "Rules"}
    assert links[1] == {"text": "Secret plan", "status": "excluded"}
    text = sidecar.format_recall([], out["hits"][:1])
    assert sidecar.links_fragment(out["hits"][0]) == " (links: PROJECTS/Voice/Voice guide.md#Rules)"
    assert text.splitlines()[-2].endswith(sidecar.links_fragment(out["hits"][0]))


def test_note_fetch_returns_chunks_in_order_and_one_section(evidence):
    whole = evidence.bundle(None, 20, "hash-ngram", path="PROJECTS/Long.md")
    starts = [h["line_start"] for h in whole["hits"]]
    assert starts == sorted(starts) and len(starts) >= 6
    assert whole["provenance"]["retrieval"] == "note"
    part = evidence.bundle(None, 20, "hash-ngram", path="PROJECTS/Long.md#Part 3")
    assert part["hits"] and all("Part 3" in h["heading_path"] for h in part["hits"])
    capped = evidence.bundle(None, 2, "hash-ngram", path="PROJECTS/Long.md")
    assert len(capped["hits"]) == 2


def test_note_fetch_refuses_alike_for_every_unserved_note(evidence):
    for ref in ("NOPE.md", "PROJECTS/Long.md#No such heading", ".obsidian/workspace.md"):
        with pytest.raises(NoteNotFound) as err:
            evidence.bundle(None, 5, "hash-ngram", path=ref)
        assert str(err.value) == "not_found"
    for bad in ("../etc/passwd", "/abs.md", "PROJECTS/"):
        with pytest.raises(retrieval.RetrievalInputError):
            evidence.bundle(None, 5, "hash-ngram", path=bad)
    with pytest.raises(ValueError):
        evidence.bundle("q", 5, "hash-ngram", path="PROJECTS/Long.md")


# ------------------------------------------------------------------ freshness

def test_each_explicit_hit_says_whether_its_note_changed(evidence, vault):
    out = evidence.bundle("tomatoes basil south bed", 3, "hash-ngram")
    assert {h["live"] for h in out["hits"]} == {"same"}
    assert out["provenance"]["indexed_at"] == BUILT_AT
    cost = out["provenance"]["live_check"]
    assert cost["stats"] <= D.LIVE_MAX_STATS and cost["hashes"] == 0
    first = out["hits"][0]["path"]
    (vault / first).write_text("# Garden\n\nEverything moved to the north bed.\n", encoding="utf-8")
    again = evidence.bundle("tomatoes basil south bed", 3, "hash-ngram")
    assert next(h["live"] for h in again["hits"] if h["path"] == first) == "changed"
    (vault / first).unlink()
    gone = evidence.bundle("tomatoes basil south bed", 3, "hash-ngram")
    assert next(h["live"] for h in gone["hits"] if h["path"] == first) == "missing"
    assert "_indexed" not in json.dumps(gone, default=str)


def test_live_check_hashes_only_when_size_matches_but_mtime_does_not(tmp_path):
    root = tmp_path / "v"
    root.mkdir()
    note = root / "n.md"
    note.write_bytes(b"hello world")
    st = os.stat(note)
    digest = __import__("hashlib").sha256(b"hello world").hexdigest()
    facts = snapshot_contract.IndexedFile(digest, st.st_mtime_ns, st.st_size)
    check = LiveCheck(root)
    hits = [{"path": "n.md", "_indexed": facts}]
    assert check.check(hits)["hashes"] == 0 and hits[0]["live"] == "same"
    os.utime(note, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))  # touched, same bytes
    hits = [{"path": "n.md", "_indexed": facts}]
    assert check.check(hits)["hashes"] == 1 and hits[0]["live"] == "same"
    note.write_bytes(b"hello WORLD")  # same size, new bytes
    hits = [{"path": "n.md", "_indexed": facts}]
    assert check.check(hits)["hashes"] == 1 and hits[0]["live"] == "changed"
    # Same size, other mtime: hash, within the cap; past it the hit stays unchecked.
    many = [{"path": "n.md", "_indexed": snapshot_contract.IndexedFile(digest, 0, len(b"hello WORLD"))}]
    many += [{"path": f"m{i}.md", "_indexed": facts} for i in range(12)]
    cost = LiveCheck(root, max_stats=4, max_hashes=1).check(many)
    assert cost["stats"] == 4 and cost["hashes"] == 1 and cost["unchecked"] == 9
    assert "live" not in many[-1] and many[1]["live"] == "missing"
    assert LiveCheck(root).check([{"path": "../x.md", "_indexed": facts}])["unchecked"] == 1


def test_recall_block_dates_the_snapshot_and_marks_a_changed_note(snap, vault, tmp_path):
    ev = ReadOnlyEvidence(ServingSnapshot(str(snap)), vault=vault)
    app = sidecar.Quoth(ledger_path=tmp_path / "l.db", evidence=ev, model="hash-ngram", gate=Gate())
    try:
        (vault / "PROJECTS/Garden.md").write_text("# Garden\n\nMoved everything north.\n", encoding="utf-8")
        out = app.prefetch({"query": "tomatoes basil south bed garden", "dry_run": True})
        lines = out["text"].splitlines()
        assert lines[1] == "Vault indexed as of 2026-10-28 03:12 UTC"
        garden = next(line for line in lines if "PROJECTS/Garden.md" in line)
        assert "[changed since indexing: read the live note]" in garden
        assert out["detail"]["provenance"]["indexed_at"] == BUILT_AT
        assert "live" in out["detail"]["timings_ms"]
    finally:
        app.close()
        ev.snapshot.close()


def test_indexed_label():
    assert sidecar.indexed_label("2026-10-28T03:12:44Z") == "2026-10-28 03:12 UTC"
    assert sidecar.indexed_label("2026-10-28T05:12:44+02:00") == "2026-10-28 03:12 UTC"
    assert sidecar.indexed_label("garbage") is None and sidecar.indexed_label(None) is None


def test_contract_reads_what_the_index_writes(tmp_path, vault):
    conn = open_db(tmp_path / "fresh.db")
    ingest_vault(conn, vault)
    try:
        facts = snapshot_contract.indexed_files(conn, ["PROJECTS/Garden.md"])["PROJECTS/Garden.md"]
        st = os.stat(vault / "PROJECTS/Garden.md")
        assert (facts.mtime_ns, facts.size, len(facts.sha256)) == (st.st_mtime_ns, st.st_size, 64)
        assert snapshot_contract.indexed_files(conn, []) == {}
        assert snapshot_contract.chunk_links(conn, [], 5) == {}
    finally:
        conn.close()
    assert snapshot_contract.manifest_built_at({"built_at": BUILT_AT}) == BUILT_AT
    assert snapshot_contract.manifest_built_at({}) is None


# ------------------------------------------------------------------ the sidecar route and the log

@pytest.fixture
def app(snap, vault, tmp_path):
    ev = ReadOnlyEvidence(ServingSnapshot(str(snap)), vault=vault)
    a = sidecar.Quoth(ledger_path=tmp_path / "l.db", evidence=ev, model="hash-ngram", gate=Gate())
    yield a
    a.close()
    ev.snapshot.close()


def test_evidence_route_validates_and_refuses_neutrally(app):
    out = app.evidence_bundle({"query": "Atlas", "path_prefix": "PROJECTS", "k": 3})
    assert out["hits"] and all(h["path"].startswith("PROJECTS/") for h in out["hits"])
    for body, code in (({"query": "Atlas", "path_prefix": "../x"}, "invalid_path_prefix"),
                       ({"path": "/etc/passwd"}, "invalid_path"),
                       ({"query": "a", "path": "PROJECTS/Long.md"}, "invalid_evidence_request"),
                       ({}, "invalid_evidence_request"),
                       ({"query": "a", "include_explicit": "yes"}, "invalid_include_explicit")):
        with pytest.raises(sidecar.SidecarError) as err:
            app.evidence_bundle(body)
        assert err.value.code == code
    with pytest.raises(sidecar.SidecarError) as err:
        app.evidence_bundle({"path": "NOPE.md"})
    assert (err.value.code, err.value.status) == ("not_found", 404)
    assert app.health()["retrieval"] == "hybrid" and app.health()["live_check"] is True


def test_prefetch_log_binds_records_to_snapshot_gate_and_retrieval(app, snap, tmp_path):
    from quoth.prefetch_log import PrefetchLog
    app.prefetch_log = PrefetchLog(tmp_path / "plog")
    app.prefetch({"query": "tomatoes basil south bed", "session_id": "s1"})
    app.prefetch_log.flush()
    (turn,) = [json.loads(x) for x in next((tmp_path / "plog").glob("*.jsonl")).read_text().splitlines()]
    assert turn["snapshot_id"] == sha(snap) and turn["retrieval"] == "hybrid"
    assert turn["gate"] == Gate().to_dict() and "live" in turn
    assert all({"via", "named"} <= set(i) for i in turn["stores"]["vault"])
    assert "tomatoes" not in json.dumps(turn)


class _LateEvidence:
    queries = object()
    retrieval = "hybrid"

    def __init__(self, delay):
        self.delay = delay

    def bundle(self, query, k, model, **_):
        time.sleep(self.delay)
        return {"hits": [{"locator": "a.md:1-2", "path": "a.md", "text": "late a", "cosine": 0.8},
                         {"locator": "a.md:3-4", "path": "a.md", "text": "late a2", "cosine": 0.7},
                         {"locator": "b.md:1-2", "path": "b.md", "text": "late b", "cosine": 0.6}],
                "provenance": {"snapshot_id": "snap-1"}}


def test_a_late_search_is_logged_when_it_finishes(tmp_path):
    from quoth.evals.online import online
    from quoth.prefetch_log import PrefetchLog
    log = PrefetchLog(tmp_path / "plog")
    app = sidecar.Quoth(ledger_path=tmp_path / "l.db", evidence=_LateEvidence(0.3), evidence_budget=0.05,
                        gate=Gate(), prefetch_log=log)
    try:
        out = app.prefetch({"query": "where is the late note", "session_id": "s1"})
        assert out["evidence_status"] == "slow"
        app.wait_idle()
        for _ in range(50):
            log.flush()
            recs = [json.loads(x) for f in (tmp_path / "plog").glob("*.jsonl") for x in f.read_text().splitlines()]
            if any(r["type"] == "late" for r in recs):
                break
            threading.Event().wait(0.02)
        late = next(r for r in recs if r["type"] == "late")
        assert late["would_inject"] == ["a.md:1-2", "b.md:1-2", "a.md:3-4"]  # one per note first
        assert late["completed_ms"] >= 250 and late["snapshot_id"] == "snap-1"
        assert late["budget_ms"] == 50.0 and "late a" not in json.dumps(late)  # locators, never text
        report = online(tmp_path / "plog")
        assert report["late"]["records"] == 1 and report["late"]["within"]["2s"] == 1.0
    finally:
        app.close()


def test_online_joins_explicit_results_to_the_last_prefetch(tmp_path):
    from quoth.evals.online import summarize
    turn = {"type": "prefetch", "ts": "1", "session": "s", "stores": {"vault": [
        {"locator": "a.md:1-2", "pool": True, "passed": True, "injected": True},
        {"locator": "b.md:1-2", "pool": True, "passed": True, "injected": False},
        {"locator": "c.md:1-2", "pool": True, "passed": False, "injected": False},
        {"locator": "d.md:1-2", "pool": False, "passed": True, "injected": False}]}}
    tools = [{"type": "tool", "ts": "2", "session": "s", "tool": "evidence_bundle", "results": ["b.md:5-6"]},
             {"type": "tool", "ts": "3", "session": "s", "tool": "evidence_bundle",
              "results": ["a.md:1-2", "z.md:1-1"]},
             {"type": "tool", "ts": "4", "session": "other", "tool": "evidence_bundle", "results": ["a.md:1-2"]}]
    join = summarize([turn, *tools])["explicit_join"]
    assert join["calls"] == 2
    assert join["rate"]["pooled_not_injected"] == 0.5 and join["rate"]["never_pooled"] == 0.5
    assert join["rate"]["injected"] == 0.5
    assert join["notes"] == {"injected": 1, "passed_not_injected": 1, "pooled_not_passed": 0,
                             "candidate_not_pooled": 0, "never_candidate": 1}


# ------------------------------------------------------------------ MCP and plugin schemas

def test_plugin_schema_exposes_filters(monkeypatch):
    from test_sidecar import REPO, _load_plugin
    plugin = _load_plugin(monkeypatch, REPO / "hermes_plugin" / "quoth")
    props = plugin.EVIDENCE_SCHEMA["parameters"]["properties"]
    assert {"query", "k", "path_prefix", "path", "include_explicit"} <= set(props)
    assert "ambiguous" in props["path_prefix"]["description"]
    assert "owner asks" in props["include_explicit"]["description"]
