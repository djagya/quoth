"""Claims gold: ledger-stable claim anchors, run-time resolution through edits and supersedes,
unresolved reporting, judged claims precision, and the label-claims / review-claims flow."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from quoth.cli import main as cli_main
from quoth.evals import InProcessTarget, load_set, run_set, validate
from quoth.evals import metrics as M
from quoth.evals.claims_gold import Resolver, resolve_rows
from quoth.evals.format import claim_anchors, row_errors, scored, write_set
from quoth.evals.label_claims import LabelError, decide, pending_rows, propose, review_claims
from quoth.ledger import core

REPO = Path(__file__).resolve().parents[1]
PUBLIC = REPO / "eval" / "public"
CLAIMS_SET = PUBLIC / "claims.jsonl"


def _claim(text, **kw):
    return {"store": "claims", "match": {"claim": {"text": text, **kw}}}


def _row(rid="r1", sources=None, **expect):
    return {"id": rid, "question": "which tea do I like", "lang": "en", "kind": "preference",
            "origin": "synthetic", "expect": {"abstain": False, "sources": sources or [_claim("Likes green tea.")],
                                              **expect}}


# ------------------------------------------------------------------ format

def test_claim_anchor_rows_validate():
    assert row_errors(_row(sources=[_claim("Likes green tea.", id=3, subject_key="tea")], claims_judged=True)) == []
    for bad in ({"id": 3}, {"text": ""}, {"text": "x", "id": 0}, {"text": "x", "id": True}, {"text": "x", "id": "3"},
                {"text": "x", "subject_key": ""}, {"text": "x", "extra": 1}, {"text": "x" * 601}, "Likes tea."):
        errors = row_errors(_row(sources=[{"store": "claims", "match": {"claim": bad}}]))
        assert errors and all(e.startswith("r1:") for e in errors), bad
    assert row_errors(_row(sources=[{"store": "vault", "match": {"claim": {"text": "x"}}}]))
    assert row_errors(_row(claims_judged="yes"))
    abstain = {"id": "a", "question": "q", "lang": "en", "kind": "unanswerable", "origin": "synthetic",
               "expect": {"abstain": True, "sources": [], "claims_judged": True}}
    assert any("claims_judged" in e for e in row_errors(abstain))


def test_pending_rows_are_not_scored_and_anchors_are_listed(tmp_path):
    pending = {**_row("p"), "provenance": {"author": "script:label-claims", "review": "pending"}}
    assert row_errors(pending) == [] and not scored(pending)
    stale = _row(stale=[_claim("Likes black tea.")], negatives=[_claim("Likes tea bags.")])
    assert [a["text"] for a in claim_anchors(stale)] == ["Likes green tea.", "Likes black tea.", "Likes tea bags."]
    write_set([_row("a", claims_judged=True), pending], tmp_path / "s.jsonl", privacy="public", fixture="any")
    loaded = load_set(tmp_path / "s.jsonl")
    assert loaded.manifest["counts"]["claims_judged"] == 1 and loaded.manifest["counts"]["review"]["pending"] == 1
    assert [r["id"] for r in loaded.select().rows] == ["a"] and loaded.select().skipped == 1


def test_public_claims_set_is_valid():
    assert validate(CLAIMS_SET) == []
    evalset = load_set(CLAIMS_SET)
    assert evalset.manifest["fixture"] == "eval/public" and evalset.manifest["privacy"] == "public"
    assert all(r["expect"]["claims_judged"] for r in evalset.rows)


# ------------------------------------------------------------------ resolution

@pytest.fixture
def ledger(tmp_path):
    """A ledger with an edit chain, a supersede chain, a forgotten, a pending and a rejected claim."""
    path = tmp_path / "ledger.db"
    conn = core.open_ledger(path)

    def add(text, kind="preference", subject_key=None, accept=True):
        cid = core.propose(conn, text=text, kind=kind, origin="assistant_proposal", proposer="t",
                           subject_key=subject_key)["claim"]["id"]
        if accept:
            core.accept(conn, cid, actor="t")
        return cid

    ids = {"tea": add("Likes green tea.")}
    ids["tea_edit"] = core.edit(conn, ids["tea"], text="Likes green tea with jasmine.", actor="t")["id"]
    ids["city"] = add("Lives in Quendorf.", kind="identity", subject_key="city")
    ids["city_new"] = add("Moved to Brask.", kind="identity", subject_key="city")
    ids["city_edit"] = core.edit(conn, ids["city_new"], text="Moved to Brask in May.", actor="t")["id"]
    ids["dog"] = add("Has a dog called Pip.", kind="identity")
    core.forget(conn, ids["dog"], reason="moved out", actor="t")
    ids["pending"] = add("Might learn the cello.", kind="taste", accept=False)
    ids["basil"] = add("Dislikes basil.", kind="taste", accept=False)
    core.reject(conn, ids["basil"], actor="t")
    # Ambiguous: "Drinks oat milk." was edited away, then proposed again and accepted.
    ids["milk"] = add("Drinks oat milk.")
    ids["milk_edit"] = core.edit(conn, ids["milk"], text="Drinks oat milk in coffee.", actor="t")["id"]
    ids["milk_again"] = add("Drinks oat milk.")
    conn.close()
    return path, ids


def test_resolution_follows_edits_and_supersedes(ledger):
    path, ids = ledger
    r = Resolver.open(path)
    try:
        got = r.resolve({"text": "likes green tea", "id": ids["tea"]})  # case and final dot aside
        assert got == {"ids": [ids["tea_edit"]], "how": "id", "steps": {"edit": 1}}
        got = r.resolve({"text": "Lives in Quendorf.", "id": 999})  # foreign id: found by text
        assert got["ids"] == [ids["city_edit"]] and got["how"] == "text"
        assert got["steps"] == {"supersede": 1, "edit": 1}
        # An id whose text differs is another ledger's claim: the text decides.
        assert r.resolve({"text": "Lives in Quendorf.", "id": ids["dog"]})["how"] == "text"
        got = r.resolve({"text": "Lives somewhere else entirely.", "subject_key": "city"})
        assert got == {"ids": [ids["city_edit"]], "how": "subject_key", "steps": {}}
        assert r.resolve({"text": "Has a dog called Pip."}) == {"ids": [], "reason": "forgotten"}
        assert r.resolve({"text": "Might learn the cello."})["reason"] == "pending"
        assert r.resolve({"text": "Dislikes basil."})["reason"] == "rejected"
        assert r.resolve({"text": "Drinks oat milk."})["reason"] == "ambiguous"
        assert r.resolve({"text": "Never existed."})["reason"] == "not_found"
        assert r.resolve({"text": "Never existed.", "subject_key": "nope"})["reason"] == "not_found"
        # Stale anchors are not followed: the stale value is that exact claim.
        assert r.resolve({"text": "Lives in Quendorf."}, follow=False)["ids"] == [ids["city"]]
    finally:
        r.close()
    assert Resolver(None).resolve({"text": "x"}) == {"ids": [], "reason": "no_ledger"}


def test_resolve_rows_reports_unresolved_apart(ledger):
    path, ids = ledger
    vault = {"store": "vault", "match": {"path": "a.md"}}
    rows = [
        _row("ok", [_claim("Likes green tea.", id=ids["tea"])], claims_judged=True,
             stale=[_claim("Lives in Quendorf.")]),
        _row("gone", [_claim("Has a dog called Pip.")], claims_judged=True),
        _row("half", [vault, _claim("Might learn the cello.")], claims_judged=True),
        _row("grouped", [vault, _claim("Moved to Brask.")],
             groups=[{"name": "v", "any_of": [vault]}, {"name": "c", "any_of": [_claim("Moved to Brask.")]}]),
        {"id": "plain", "question": "q", "lang": "en", "kind": "fact", "origin": "synthetic",
         "expect": {"abstain": False, "sources": [vault]}},
    ]
    r = Resolver.open(path)
    try:
        out, report = resolve_rows(rows, r)
    finally:
        r.close()
    assert "plain" not in out and out["gone"] is None
    assert out["ok"]["expect"]["sources"] == [{"store": "claims", "match": {"locator": f"claim:{ids['tea_edit']}"}}]
    assert out["ok"]["expect"]["stale"] == [{"store": "claims", "match": {"locator": f"claim:{ids['city']}"}}]
    assert out["ok"]["expect"]["claims_judged"] is True
    assert out["half"]["expect"]["sources"] == [vault] and out["half"]["expect"]["claims_judged"] is False
    assert [g["any_of"] for g in out["grouped"]["expect"]["groups"]][1] == [
        {"store": "claims", "match": {"locator": f"claim:{ids['city_edit']}"}}]
    assert report == {"anchors": 4, "resolved": 2, "by": {"id": 1, "text": 1}, "followed": {"edit": 2, "supersede": 0},
                      "unresolved": {"forgotten": 1, "pending": 1}, "rows_excluded": ["gone"],
                      "rows_partial": ["half"], "ledger": True, "stale": {"resolved": 1, "unresolved": 0}}
    # Without a ledger every anchor is unresolved, never a miss.
    out, report = resolve_rows(rows, None)
    assert report["unresolved"] == {"no_ledger": 4} and report["ledger"] is False and out["ok"] is None
    assert resolve_rows([rows[-1]], None) == ({}, None)


# ------------------------------------------------------------------ metrics

def test_judged_claims_precision():
    def obs(rid, injected, matched, judged=True):
        items = [{"id": i, "text": f"c{i}", "injected": True} for i in range(injected)]
        sources = [{"store": "claims", "match": {"locator": f"claim:{i}"}} for i in range(matched)] or \
                  [{"store": "vault", "match": {"path": "a.md"}}]
        row = {"id": rid, "kind": "fact", "origin": "synthetic", "lang": "en",
               "expect": {"abstain": False, "sources": sources, "claims_judged": judged}}
        return M.observe(row, {"claims": items})
    block = M.block([obs("a", 2, 1), obs("b", 0, 0), obs("c", 1, 0), obs("d", 3, 0, judged=False)])
    claims = block["stores"]["claims"]
    assert claims["judged_n"] == 3 and claims["judged_precision"] == 0.25 and claims["judged_items_mean"] == 1.0
    assert claims["n"] == 1 and claims["hit@5"] == 1.0 and claims["injected_hit_rate"] == 1.0
    assert "judged_n" not in M.block([obs("d", 3, 0, judged=False)])["stores"]["claims"]
    # An anchor scored without resolution matches the labelled text.
    item = {"id": 7, "text": "Likes green tea"}
    assert M.matches(item, "claims", _claim("likes green tea.", id=1))
    assert not M.matches(item, "claims", _claim("Likes black tea."))


# ------------------------------------------------------------------ runs

def test_run_resolves_claims_gold_through_an_edit(ledger, tmp_path):
    path, ids = ledger
    write_set([_row("tea", [_claim("Likes green tea.", id=ids["tea"])], claims_judged=True),
               _row("dog", [_claim("Has a dog called Pip.")], claims_judged=True)],
              tmp_path / "s.jsonl", privacy="private", fixture="t")
    from quoth.relevance import Gate
    target = InProcessTarget.build(ledger=str(path), model="hash-ngram", gate=Gate())
    try:
        receipt, _ = run_set(load_set(tmp_path / "s.jsonl"), target, explicit=False)
    finally:
        target.close()
    data = receipt.data
    assert data["claims_gold"]["followed"] == {"edit": 1, "supersede": 0}
    assert data["claims_gold"]["rows_excluded"] == ["dog"] and data["scored"] == 1
    claims = data["metrics"]["overall"]["stores"]["claims"]
    assert claims["n"] == 1 and claims["hit@10"] == 1.0 and claims["judged_n"] == 1
    assert "green tea" not in receipt.to_json().lower()


@pytest.fixture(scope="module")
def public_run(tmp_path_factory):
    from quoth.evals.fixture import build_fixture
    from quoth.relevance import Gate
    built = build_fixture(PUBLIC, tmp_path_factory.mktemp("public-claims"))
    target = InProcessTarget.build(**built["paths"], model=built["model"], gate=Gate(), retrieval="dense")
    try:
        receipt, _ = run_set(load_set(CLAIMS_SET), target)
    finally:
        target.close()
    return receipt.data, built


def test_public_claims_set_fills_the_claims_metrics(public_run):
    data, _ = public_run
    report = data["claims_gold"]
    assert report["resolved"] == 4 and report["unresolved"] == {} and report["rows_excluded"] == []
    assert report["by"] == {"id": 2, "subject_key": 1, "text": 1} and report["followed"]["supersede"] == 2
    assert report["stale"] == {"resolved": 1, "unresolved": 0}
    claims = data["metrics"]["overall"]["stores"]["claims"]
    assert claims["n"] == 4 and claims["judged_n"] == 6
    for key in ("hit@5", "injected_hit_rate"):
        assert 0.0 <= claims[key] <= 1.0
    assert data["metrics"]["overall"]["stale_leak"] == 0
    blob = json.dumps(data)
    for row in load_set(CLAIMS_SET).rows:
        for anchor in claim_anchors(row):
            assert anchor["text"] not in blob


def test_sweep_reports_judged_claims_precision(public_run):
    from quoth.evals.sweep import sweep
    data, _ = public_run
    result = sweep(data)
    assert "claims_judged_precision" in result["baseline"]


# ------------------------------------------------------------------ label-claims / review-claims

def _set_and_ledger(tmp_path, ledger):
    path, _ = ledger
    rows = [
        {"id": "q1", "question": "Which green tea do I like?", "lang": "en", "kind": "preference",
         "origin": "real_user", "expect": {"abstain": False, "sources": [{"store": "vault", "match": {"path": "a.md"}}]},
         "provenance": {"author": "owner", "review": "edited"}},
        {"id": "q2", "question": "Where have I moved to?", "lang": "en", "kind": "fact", "origin": "real_user",
         "expect": {"abstain": False, "sources": [{"store": "vault", "match": {"path": "b.md"}}]}},
        {"id": "q3", "question": "Is there oat milk?", "lang": "en", "kind": "fact", "origin": "real_user",
         "expect": {"abstain": False, "sources": [{"store": "vault", "match": {"path": "c.md"}}]}},
        {"id": "q4", "question": "zzz qqq", "lang": "en", "kind": "fact", "origin": "real_user",
         "expect": {"abstain": False, "sources": [{"store": "vault", "match": {"path": "d.md"}}]}},
        {"id": "q5", "question": "What is my blood type?", "lang": "en", "kind": "unanswerable",
         "origin": "real_user", "expect": {"abstain": True, "sources": []}},
        _row("q6", [_claim("Likes green tea.")]),
    ]
    write_set(rows, tmp_path / "set.jsonl", privacy="private", fixture="private")
    return tmp_path / "set.jsonl", path


def test_label_claims_proposes_pending_rows_only(tmp_path, ledger):
    set_path, ledger_path = _set_and_ledger(tmp_path, ledger)
    out = tmp_path / "labelled.jsonl"
    summary = propose(set_path, ledger_path, out, model=None)
    assert summary["scorer"] == "lexical" and summary["pending"] == 3 and summary["no_candidates"] == 1
    assert summary["abstain"] == 1 and summary["already_labelled"] == 1 and summary["claims"] == 4
    assert oct(out.stat().st_mode & 0o777) == "0o600" and load_set(out).manifest["privacy"] == "private"
    rows = load_set(out).rows
    q1 = rows[0]
    assert q1["provenance"] == {"author": "owner", "review": "pending"} and not scored(q1)
    label = q1["meta"]["claims_label"]
    assert label["state"] == "pending" and label["prior_provenance"] == {"author": "owner", "review": "edited"}
    assert label["candidates"][0]["text"] == "Likes green tea with jasmine."
    assert rows[3] == load_set(set_path).rows[3]  # no candidates: unchanged, still scored
    assert [r["id"] for r in load_set(out).select().rows] == ["q4", "q5", "q6"]
    with pytest.raises(LabelError):
        propose(set_path, ledger_path, set_path, model=None)
    dense = propose(set_path, ledger_path, tmp_path / "dense.jsonl", model="hash-ngram", top=2)
    assert dense["scorer"] == "dense:hash-ngram" and dense["pending"] == 4
    assert all(len(r["meta"]["claims_label"]["candidates"]) == 2 for r in load_set(tmp_path / "dense.jsonl").rows
               if r.get("meta", {}).get("claims_label"))


def test_label_claims_falls_back_to_words_when_the_model_is_missing(tmp_path, ledger, monkeypatch):
    from quoth.embed import backends
    set_path, ledger_path = _set_and_ledger(tmp_path, ledger)

    def unavailable(name, **kw):
        raise backends.EmbedderUnavailable("not installed")
    monkeypatch.setattr(backends, "get_embedder", unavailable)
    summary = propose(set_path, ledger_path, tmp_path / "out.jsonl")
    assert summary["scorer"] == "lexical" and summary["dense_unavailable"] == "EmbedderUnavailable"


def test_review_claims_confirms_only_what_the_owner_picks(tmp_path, ledger):
    set_path, ledger_path = _set_and_ledger(tmp_path, ledger)
    out = tmp_path / "labelled.jsonl"
    propose(set_path, ledger_path, out, model=None)
    said = []
    # q1: "9" is out of range, then claim 1; q2: none is right; q3: skip.
    answers = iter(["9", "1", "n", "s"])
    summary = review_claims(out, ledger=ledger_path, ask=lambda _: next(answers), say=said.append)
    assert summary == {"pending": 3, "confirmed": 1, "none_right": 1, "dismissed": 0, "skipped": 1, "remaining": 1}
    assert any("no claim numbered 9" in str(s) for s in said)
    rows = {r["id"]: r for r in load_set(out).rows}
    q1 = rows["q1"]
    assert q1["provenance"] == {"author": "owner", "review": "edited"} and scored(q1)
    assert q1["expect"]["claims_judged"] is True
    anchor = q1["expect"]["sources"][-1]["match"]["claim"]
    assert anchor["text"] == "Likes green tea with jasmine." and isinstance(anchor["id"], int)
    assert q1["meta"]["claims_label"]["state"] == "confirmed" and "candidates" not in q1["meta"]["claims_label"]
    assert "provenance" not in rows["q2"] and rows["q2"]["expect"]["claims_judged"] is True
    assert [s["store"] for s in rows["q2"]["expect"]["sources"]] == ["vault"]
    assert pending_rows(list(rows.values())) == [2]
    # Dismiss the last one: its provenance returns, it is not judged.
    review_claims(out, ask=lambda _: "d", say=said.append)
    q3 = {r["id"]: r for r in load_set(out).rows}["q3"]
    assert "claims_judged" not in q3["expect"] and q3["meta"]["claims_label"]["state"] == "dismissed" and scored(q3)
    # The confirmed set resolves against the ledger it was labelled on.
    r = Resolver.open(ledger_path)
    try:
        _, report = resolve_rows(load_set(out).rows, r)
    finally:
        r.close()
    assert report["resolved"] == 2 and report["unresolved"] == {}


def test_review_claims_refuses_a_claim_no_longer_current(tmp_path, ledger):
    set_path, ledger_path = _set_and_ledger(tmp_path, ledger)
    out = tmp_path / "labelled.jsonl"
    propose(set_path, ledger_path, out, model=None)
    row = load_set(out).rows[0]
    conn = core.open_ledger(ledger_path)
    try:
        core.forget(conn, row["meta"]["claims_label"]["candidates"][0]["id"], reason="t", actor="t")
    finally:
        conn.close()
    said = []
    answers = iter(["1", "q"])
    summary = review_claims(out, ledger=ledger_path, ask=lambda _: next(answers), say=said.append)
    assert summary["confirmed"] == 0 and any("no longer accepted" in str(s) for s in said)
    assert decide(row, None)["meta"]["claims_label"]["state"] == "dismissed"


def test_label_claims_cli_prints_counts_only(tmp_path, ledger, capsys):
    set_path, ledger_path = _set_and_ledger(tmp_path, ledger)
    out = tmp_path / "labelled.jsonl"
    assert cli_main(["eval", "label-claims", str(set_path), "--ledger", str(ledger_path), "--out", str(out),
                     "--lexical"]) == 0
    printed = capsys.readouterr().out
    assert json.loads(printed)["pending"] == 3 and "green tea" not in printed and "Which" not in printed
    assert cli_main(["eval", "label-claims", str(set_path), "--ledger", str(tmp_path / "nope.db"),
                     "--out", str(tmp_path / "x.jsonl")]) == 2
    assert cli_main(["eval", "validate", str(out)]) == 0


def test_a_socket_run_resolves_against_its_ledger_read_only(ledger, tmp_path):
    import sqlite3
    from types import SimpleNamespace
    from quoth.evals.run import RunError, claims_resolver
    path, ids = ledger
    before = path.read_bytes()
    r = claims_resolver(SimpleNamespace(ledger=str(path)))
    try:
        assert r.resolve({"text": "Likes green tea."})["ids"] == [ids["tea_edit"]]
        with pytest.raises(sqlite3.OperationalError):
            r.conn.execute("DELETE FROM claims")  # opened read-only
    finally:
        r.close()
    assert path.read_bytes() == before
    assert claims_resolver(SimpleNamespace(ledger=None)).conn is None
    with pytest.raises(RunError):
        claims_resolver(SimpleNamespace(ledger=str(tmp_path / "missing.db")))
    (tmp_path / "junk.db").write_text("not a ledger")
    with pytest.raises(RunError):
        claims_resolver(SimpleNamespace(ledger=str(tmp_path / "junk.db")))
