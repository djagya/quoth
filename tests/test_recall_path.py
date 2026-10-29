"""The recall decision path: one query vector per turn, one lexical measure (Russian
included), the gate before the cut, the vault status line, use_count after the gate, and
the consistency fixes (episode floor basis, k_claims, tie-breaks)."""
from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from quoth import recall_defaults as D
from quoth import relevance, sidecar
from quoth.claim_index import ClaimIndex
from quoth.ledger import core
from quoth.relevance import Gate
from quoth.serving_cache import QueryEmbedder

from test_sidecar import state_db  # noqa: F401  (fixture)


# ------------------------------------------------------------------ one query vector per turn

class _CountingEmbedder:
    def __init__(self, delay=0.1, fail=False):
        self.delay, self.fail = delay, fail
        self.calls = 0
        self._lock = threading.Lock()

    def embed_query(self, text):
        with self._lock:
            self.calls += 1
        time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("embed failed")
        return [1.0, 0.0, 0.0]


def _concurrent(fn, n):
    out, errors = [None] * n, []
    start = threading.Barrier(n)

    def run(i):
        start.wait()
        try:
            out[i] = fn()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    return out, errors


def test_concurrent_cold_calls_embed_the_query_once():
    queries, emb = QueryEmbedder(), _CountingEmbedder()
    out, errors = _concurrent(lambda: queries.vector("m", emb, "where is the bike"), 6)
    assert not errors and emb.calls == 1
    assert all(np.array_equal(v, out[0]) for v in out)
    queries.vector("m", emb, "where is the bike")  # warm: no embed
    assert emb.calls == 1
    queries.vector("m", emb, "another query")  # another key embeds on its own
    assert emb.calls == 2


def test_a_waiter_embeds_itself_when_the_leader_is_late_or_fails():
    late = QueryEmbedder(wait=0.05)
    emb = _CountingEmbedder(delay=0.3)
    _, errors = _concurrent(lambda: late.vector("m", emb, "q"), 3)
    assert not errors and emb.calls == 3  # nobody waits past `wait`
    failing = QueryEmbedder()
    bad = _CountingEmbedder(delay=0.1, fail=True)
    _, errors = _concurrent(lambda: failing.vector("m", bad, "q"), 3)
    assert len(errors) == 3 and bad.calls >= 2  # a failure is never shared as a result
    assert failing._flights == {}


def test_uncached_calls_skip_the_flight():
    queries, emb = QueryEmbedder(), _CountingEmbedder(delay=0.0)
    queries.vector("m", emb, "q", cache=False)
    queries.vector("m", emb, "q", cache=False)
    assert emb.calls == 2 and not queries._vectors


# ------------------------------------------------------------------ one lexical measure

@pytest.fixture
def conn(tmp_path):
    c = core.open_ledger(tmp_path / "ledger.db")
    yield c
    c.close()


def _accept(conn, text, kind="preference"):
    cid = core.propose(conn, text=text, kind=kind, origin="assistant_proposal", proposer="t", reason="r")["claim"]["id"]
    core.accept(conn, cid, actor="t")
    return cid


def test_function_words_alone_never_match_a_claim(conn):
    _accept(conn, "The bike is in the garage.")
    assert core.recall(conn, "what is the weather in the city") == []
    assert core.recall(conn, "is it in the") == []
    assert [c["text"] for c in core.recall(conn, "where is my bike")] == ["The bike is in the garage."]


def test_lexical_claims_rank_by_coverage_then_newest(conn):
    old = _accept(conn, "Alex drinks green tea.")
    new = _accept(conn, "Alex drinks black tea.")
    both = _accept(conn, "Green tea in the morning, always.")
    # 3 of 3 words, then 2 of 3 (green, tea), then 1 of 3 (tea).
    assert [c["id"] for c in core.recall(conn, "green tea morning")] == [both, old, new]
    # Equal coverage: newest first.
    assert [c["id"] for c in core.recall(conn, "tea")] == [both, new, old]


@pytest.mark.parametrize("query,claim", [
    ("Где стоит мой велосипед?", "Велосипед стоит в гараже у дома."),
    ("сколько стоит аренда квартиры", "Аренда квартиры в Москве — 80 тысяч."),
    ("когда у мамы день рождения", "День рождения мамы — 12 марта."),
    ("какую машину я хотел купить", "Хочу купить машину до лета."),
    ("в каком городе живёт брат", "Брат живет в Казани, в своём городе."),
])
def test_russian_inflections_match(conn, query, claim):
    _accept(conn, claim)
    assert [c["text"] for c in core.recall(conn, query)] == [claim]


@pytest.mark.parametrize("query", [
    "что это такое и как это было",
    "который из них был тогда",
    "мне это очень нужно, спасибо",
    "а где же вот это всё",
])
def test_russian_function_words_match_nothing(conn, query):
    _accept(conn, "Велосипед стоит в гараже у дома.")
    _accept(conn, "Это было в прошлом году, когда мы были в Казани.")
    assert relevance.terms(query) == frozenset(), sorted(relevance.terms(query))
    assert core.recall(conn, query) == []


def test_russian_coverage_counts_content_words_only():
    q = relevance.terms("Где я оставил ключи от квартиры?")
    assert q == {relevance.stem("оставил"), relevance.stem("ключи"), relevance.stem("квартиры")}
    assert relevance.coverage(q, "Ключи от квартиры лежат у соседа.") == round(2 / 3, 4)
    assert relevance.coverage(q, "Я оставила ключ в машине.") == round(2 / 3, 4)  # оставила, ключ


# ------------------------------------------------------------------ use_count after the gate

class _Hits:
    queries = object()

    def __init__(self, hits):
        self.hits = hits

    def bundle(self, query, k, model, with_scores=False):
        return {"hits": [dict(h) for h in self.hits[:k]]}


def _app(tmp_path, state_db, gate=None, hits=(), evidence=True, **kw):  # noqa: F811
    return sidecar.Quoth(ledger_path=tmp_path / "l.db", state_db=state_db,
                         evidence=_Hits(list(hits)) if evidence else None, evidence_budget=5.0,
                         gate=Gate() if gate is None else gate, **kw)


def test_use_count_counts_only_injected_claims(tmp_path, state_db):  # noqa: F811
    app = _app(tmp_path, state_db, Gate(claims_min_lexical=0.5))
    try:
        with app.lock:
            bike = _accept(app.conn, "The bike is in the garage.")
            tea = _accept(app.conn, "Alex drinks green tea in the morning.")
            rose = _accept(app.conn, "The garden has a tea rose.")
        out = app.prefetch({"query": "what is the weather in the city"})  # function words + nothing
        assert out["claims"] == 0
        app.prefetch({"query": "green tea morning"})
        with app.lock:
            used = {r[0]: r[1] for r in app.conn.execute("SELECT id, use_count FROM claims")}
        assert used == {bike: 0, tea: 1, rose: 0}  # the rose shares one word of three: gated
        app.prefetch({"query": "green tea morning", "dry_run": True})  # dry runs never count
        with app.lock:
            assert app.conn.execute("SELECT use_count FROM claims WHERE id = ?", (tea,)).fetchone()[0] == 1
    finally:
        app.close()


def test_a_gated_claim_slot_is_refilled_from_the_pool(tmp_path, state_db, monkeypatch):  # noqa: F811
    """Gate before the cut: dense claims under the floor give their slots to the next candidates."""
    app = _app(tmp_path, state_db, Gate(claims_min_cosine=0.8), hits=[{"locator": "a.md:1-1", "text": "x"}])
    try:
        with app.lock:
            weak = [_accept(app.conn, f"Weak meaning match number {i}.") for i in range(6)]
            strong = _accept(app.conn, "Strong meaning match.")
        dense = [(cid, 0.6) for cid in weak] + [(strong, 0.9)]
        monkeypatch.setattr(app, "_dense_work", lambda q, k, k_claims: {"hits": [], "claims": dense[:k_claims]})
        out = app.prefetch({"query": "zzz unrelated", "dry_run": True})
        assert out["claims"] == 1 and "Strong meaning match." in out["text"]
        cands = out["detail"]["candidates"]["claims"]
        assert [c["pool"] for c in cands] == [True] * 7 and [c["injected"] for c in cands][-1]
    finally:
        app.close()


def test_k_claims_bounds_dense_and_lexical_claims(tmp_path, state_db, monkeypatch):  # noqa: F811
    app = _app(tmp_path, state_db, hits=[{"locator": "a.md:1-1", "text": "x"}])
    try:
        with app.lock:
            ids = [_accept(app.conn, f"Tea fact number {i}.") for i in range(10)]
        monkeypatch.setattr(app, "_dense_work",
                            lambda q, k, k_claims: {"hits": [], "claims": [(c, 0.9) for c in ids[:k_claims]]})
        assert app.prefetch({"query": "tea", "dry_run": True, "k_claims": 2})["claims"] == 2
        # More than the default 6 dense claims when the caller asks for them.
        out = app.prefetch({"query": "tea", "dry_run": True, "k_claims": 8})
        assert out["claims"] == 8
        assert all(c["via"] == "dense" for c in out["detail"]["candidates"]["claims"] if c["injected"])
    finally:
        app.close()


def test_dense_and_lexical_ties_both_break_newest_first(tmp_path):
    index = ClaimIndex(min_similarity=0.0)
    for cid in (3, 1, 2):
        index._vectors[cid] = (f"t{cid}", np.array([1.0, 0.0], dtype=np.float32))
    assert [cid for cid, _ in index.rank(np.array([1.0, 0.0]), 3)] == [3, 2, 1]


# ------------------------------------------------------------------ the status line

HITS = [{"locator": "a.md:1-2", "text": "Feed the sourdough starter at 1:5:5.", "note_date": None, "cosine": 0.71}]


def test_shown_evidence_carries_the_standing_hint(tmp_path, state_db):  # noqa: F811
    app = _app(tmp_path, state_db, hits=HITS)
    try:
        text = app.prefetch({"query": "feed the starter"})["text"]
        lines = text.splitlines()
        assert lines[1] == sidecar.VAULT_HEADER and lines[2] == sidecar.VAULT_HINT
        assert "Vault evidence:" not in text
    finally:
        app.close()


def test_an_unavailable_vault_says_so(tmp_path, state_db):  # noqa: F811
    class _Broken(_Hits):
        def bundle(self, *a, **k):
            raise RuntimeError("no snapshot")

    app = sidecar.Quoth(ledger_path=tmp_path / "l.db", state_db=state_db, evidence=_Broken([]),
                        evidence_budget=5.0)
    try:
        out = app.prefetch({"query": "feed the starter"})
        assert out["evidence_status"] == "unavailable"
        assert out["text"] == f"<quoth-recall>\n{sidecar.VAULT_NOTES['unavailable']}\n</quoth-recall>"
    finally:
        app.close()


def test_no_vault_configured_adds_no_line(tmp_path, state_db):  # noqa: F811
    app = _app(tmp_path, state_db, evidence=False)
    try:
        assert app.prefetch({"query": "feed the starter"})["text"] == ""
    finally:
        app.close()


def test_the_serving_default_gate_reports_none_relevant(tmp_path, state_db):  # noqa: F811
    weak = [{"locator": "w.md:1-1", "text": "Unrelated words.", "note_date": None, "cosine": 0.31}]
    app = sidecar.Quoth(ledger_path=tmp_path / "l.db", state_db=state_db, evidence=_Hits(weak), evidence_budget=5.0)
    try:
        assert app.gate == relevance.default_gate()
        out = app.prefetch({"query": "capital of Peru", "dry_run": True})
        assert out["evidence"] == 0 and out["detail"]["vault_note"] == "none_relevant"
        assert sidecar.VAULT_NOTES["none_relevant"] in out["text"]
        view = sidecar.render_view(app, "recall capital of Peru")
        assert "none relevant" in view
    finally:
        app.close()


# ------------------------------------------------------------------ episode floor basis

def test_the_episode_floor_compares_the_reported_score(tmp_path):
    from quoth import episodes as ep
    from test_episodes import TOMATO, _add, _schema  # noqa: F401
    import sqlite3
    state = tmp_path / "state.db"
    db = sqlite3.connect(state)
    _schema(db)
    db.execute("INSERT INTO sessions(id, source) VALUES ('tg1', 'telegram')")
    db.commit()
    db.close()
    _add(state, [(2, "tg1", "assistant", 1, 0, 1_760_000_010.0, TOMATO)])
    e = ep.Episodes(tmp_path / "e.db", model="hash-ngram")
    try:
        e.ingest(state)
        e.embed(limit=100)
        ids, mat = e._load_matrix()
        target = 0.61996  # reported as 0.62: it must pass a 0.62 floor live as on a dry run
        unit = mat[0] / np.linalg.norm(mat[0])
        other = np.zeros_like(unit)
        other[int(np.argmin(np.abs(unit)))] = 1.0
        other -= unit * float(other @ unit)
        other /= np.linalg.norm(other)
        q = target * unit + np.sqrt(1 - target ** 2) * other
        assert round(float(mat[0] @ q), 4) == 0.62 and float(mat[0] @ q) < 0.62
        hits = e.search("x", k=2, min_score=0.62, query_vector=q)["hits"]
        assert [h["score"] for h in hits] == [0.62]
    finally:
        e.close()


# ------------------------------------------------------------------ flags

def test_the_assistant_lane_floor_has_a_sidecar_flag(monkeypatch):
    seen = {}
    monkeypatch.setattr(sidecar, "Quoth", lambda **kw: seen.update(kw) or (_ for _ in ()).throw(SystemExit(0)))
    with pytest.raises(SystemExit):
        sidecar.main(["--ledger", ":memory:", "--state-db", "", "--assistant-min-score", "0.7"])
    assert seen["episode_min_score"] == 0.7 and seen["owner_min_score"] == D.OWNER_MIN_SCORE
    seen.clear()
    with pytest.raises(SystemExit):
        sidecar.main(["--ledger", ":memory:", "--state-db", ""])
    assert seen["episode_min_score"] == D.ASSISTANT_MIN_SCORE and seen["gate"] == relevance.default_gate()
