"""Past values in recall: the past-intent detector, the ledger's past values, the prefetch
path (gate, cap, flag, no extra query on other turns), formatting, and the eval follow modes."""
from __future__ import annotations

from datetime import date
import json

import pytest

from quoth import history, relevance, sidecar
from quoth.evals import metrics as M
from quoth.evals import sweep as SW
from quoth.evals.claims_gold import Resolver, resolve_rows
from quoth.evals.format import row_errors, write_set
from quoth.evals.label_claims import anchor, propose
from quoth.ledger import core

TODAY = date(2026, 11, 6)


# ------------------------------------------------------------------ past intent

@pytest.mark.parametrize("text", [
    "Where did I live before?",
    "What car did I have before the Tesla?",
    "Where did we used to go on Fridays?",
    "I used to drink black tea, right?",
    "What was my home city?",
    "What were the server names back then?",
    "Which laptop did I use previously?",
    "What did I say earlier about the move?",
    "What did I work on in 2019?",
    "What was the plan on 2026-03-01?",
    "Where did I work last year?",
    "Где я жил раньше?",
    "Раньше я пил чай, что это был за сорт?",
    "Какой сервер был до этого?",
    "Где я работал в 2020 году?",
    "Что было в прошлом году?",
    "Wo habe ich früher gewohnt?",
    "Früher hatte ich ein Auto, welches?",
    "Wie hieß der Server damals?",
    "Was war vorher mein Lieblingstee?",
])
def test_past_questions_are_detected(text):
    assert history.asks_past(text, TODAY), text


@pytest.mark.parametrize("text", [
    "Before I go, where do I live?",
    "Remind me before the meeting starts.",
    "Can we meet earlier tomorrow?",
    "I'm getting used to the new keyboard.",
    "Which tool is used to build the site?",
    "What is my home city?",
    "What will I do in 2027?",
    "What is planned for 2026?",
    "Schedule it for 2026-12-01.",
    "Приходи раньше, пожалуйста.",
    "Прежде всего, спасибо!",
    "Прежде чем начать, проверь почту.",
    "Сделай это до того, как я уйду.",
    "Kannst du früher kommen?",
    "Ruf mich vorher an.",
    "Früher als geplant, bitte.",
    "",
    "thanks",
])
def test_present_and_future_turns_are_not_past(text):
    assert history.past_cue(text, TODAY) is None, (text, history.past_cue(text, TODAY))


def test_flag_overrides_the_detector():
    assert history.wants_history(None, "Where did I live before?", TODAY) == "intent"
    assert history.wants_history(None, "Where do I live?", TODAY) is None
    assert history.wants_history(True, "Where do I live?", TODAY) == "flag"
    assert history.wants_history(False, "Where did I live before?", TODAY) is None


def test_interleave_keeps_each_current_first_and_the_cap():
    currents = [{"id": 1}, {"id": 2}, {"id": 3}]
    past = {1: [{"id": 11}, {"id": 12}, {"id": 13}], 3: [{"id": 31}]}
    ids = [c["id"] for c in history.interleave(currents, past, cap=6, per_claim=2)]
    assert ids == [1, 11, 12, 2, 3, 31]
    assert [c["id"] for c in history.interleave(currents, past, cap=3, per_claim=3)] == [1, 11, 12]
    assert [c["id"] for c in history.interleave(currents, {}, cap=2, per_claim=3)] == [1, 2]
    assert history.span({"valid_from": "2026-03-01T10:00:00.000Z", "valid_to": "2026-10-01T09:00:00.000Z"}) \
        == "from 2026-03-01 until 2026-10-01"
    assert history.span({"valid_to": "2026-10-01T09:00:00.000Z"}) == "until 2026-10-01"


# ------------------------------------------------------------------ ledger past values

T = ["2026-0%d-01T10:00:00.000Z" % m for m in range(1, 10)]


def _add(conn, text, *, key=None, now, accept=True, kind="environment", supersedes=None):
    cid = core.propose(conn, text=text, kind=kind, origin="assistant_proposal", proposer="t",
                       subject_key=key, supersedes=supersedes, now=now)["claim"]["id"]
    if accept:
        core.accept(conn, cid, actor="t", now=now)
    return cid


@pytest.fixture
def conn(tmp_path):
    c = core.open_ledger(tmp_path / "ledger.db")
    yield c
    c.close()


def test_history_walks_supersedes_newest_first_and_follows_edits(conn):
    anvil = _add(conn, "The build server is called anvil.", key="srv", now=T[0])
    anvil2 = core.edit(conn, anvil, text="The build server is called Anvil.", actor="t", now=T[1])["id"]
    forge = _add(conn, "The build server is called forge.", key="srv", now=T[2])
    crucible = _add(conn, "The build server is called crucible.", key="srv", now=T[3])
    crucible2 = core.edit(conn, crucible, text="The build server is crucible.", actor="t", now=T[4])["id"]
    got = core.history(conn, [crucible2])
    # The current value's own old wording (crucible) is not a past value; anvil is shown once,
    # in its latest wording, valid from its first wording's acceptance.
    assert [p["id"] for p in got[crucible2]] == [forge, anvil2]
    assert got[crucible2][0]["superseded_by"] == crucible and got[crucible2][1]["superseded_by"] == forge
    assert got[crucible2][1]["valid_from"] == T[0] and got[crucible2][1]["valid_to"] == T[2]
    assert got[crucible2][0]["valid_from"] == T[2] and got[crucible2][0]["valid_to"] == T[3]
    assert all(p["history_of"] == crucible2 and p["current"] is False for p in got[crucible2])
    # Not current: no entry. Unknown ids and empty input: nothing.
    assert core.history(conn, [forge, anvil, 999]) == {} and core.history(conn, []) == {}


def test_history_never_shows_forgotten_pending_rejected_or_expired(conn):
    # Forgotten before the newer fact was accepted: the supersede never applied.
    a = _add(conn, "Has a dog called Pip.", now=T[0], kind="identity")
    b = _add(conn, "Has a cat called Mo.", now=T[1], kind="identity", supersedes=a, accept=False)
    core.forget(conn, a, reason="gone", actor="t", now=T[2])
    core.accept(conn, b, actor="t", now=T[3])
    # An older pending proposal for the slot expired (latest wins), then the newer one was accepted.
    p1 = _add(conn, "Works at Quill.", key="job", now=T[0], accept=False, kind="identity")
    p2 = _add(conn, "Works at Ember.", key="job", now=T[1], kind="identity")
    assert core.get(conn, p1)["review_status"] == "expired"
    # A rejected newer fact leaves the old one current, with no past.
    c = _add(conn, "Lives in Quendorf.", key="city", now=T[0], kind="identity")
    r = _add(conn, "Lives in Brask.", key="city", now=T[1], kind="identity", accept=False)
    core.reject(conn, r, actor="t", now=T[2])
    assert core.history(conn, [b, p2, c]) == {}
    # A superseded value marked forgotten by hand is still never shown.
    old = _add(conn, "Drinks oat milk.", key="milk", now=T[0], kind="preference")
    new = _add(conn, "Drinks soy milk.", key="milk", now=T[1], kind="preference")
    assert [p["id"] for p in core.history(conn, [new])[new]] == [old]
    conn.execute("UPDATE claims SET forget_reason = 'x' WHERE id = ?", (old,))
    assert core.history(conn, [new]) == {}


# ------------------------------------------------------------------ prefetch

@pytest.fixture
def app(tmp_path):
    a = sidecar.Quoth(ledger_path=tmp_path / "l.db", gate=relevance.Gate())
    yield a
    a.close()


def _city_chain(app):
    with app.lock:
        old = _add(app.conn, "Platypus lives in Quendorf.", key="city", now=T[0], kind="identity")
        mid = _add(app.conn, "Platypus lives in Brask.", key="city", now=T[2], kind="identity")
        new = _add(app.conn, "Platypus lives in Zanzibar.", key="city", now=T[4], kind="identity")
    return old, mid, new


def test_past_turn_injects_past_values_under_their_claim(app):
    old, mid, new = _city_chain(app)
    out = app.prefetch({"query": "Where did Platypus live before?", "dry_run": True})
    assert out["history"] == {"asked": "intent", "items": 2} and out["claims"] == 3
    assert [c["id"] for c in out["detail"]["claims"]] == [new, mid, old]
    assert out["detail"]["claims"][1]["history_of"] == new
    text = out["text"]
    assert sidecar.CLAIMS_HEADER_PAST in text
    lines = text.splitlines()
    i = lines.index(f"- #{new} identity: Platypus lives in Zanzibar. [inferred]")
    assert lines[i + 1] == (f"  - #{mid} previously (from 2026-03-01 until 2026-05-01; replaced by #{new},"
                            f" not current): Platypus lives in Brask. [inferred]")
    assert lines[i + 2].startswith(f"  - #{old} previously (from 2026-01-01 until 2026-03-01; replaced by #{mid}")
    cands = out["detail"]["candidates"]["claims"]
    assert [(c["id"], c["via"], c.get("history_of")) for c in cands] == [
        (new, "lexical", None), (mid, "history", new), (old, "history", new)]
    assert all(c["injected"] for c in cands)
    # The use count is for current claims only.
    live = app.prefetch({"query": "Where did Platypus live before?"})
    assert live["claims"] == 3
    with app.lock:
        assert core.get(app.conn, new)["use_count"] == 1 and core.get(app.conn, mid)["use_count"] == 0


def test_present_turn_is_unchanged_and_runs_no_history_query(app, monkeypatch):
    _city_chain(app)
    calls = []
    real = core.history
    monkeypatch.setattr(core, "history", lambda *a, **k: calls.append(1) or real(*a, **k))
    out = app.prefetch({"query": "Where does Platypus live?", "dry_run": True})
    assert calls == [] and "history" not in out and out["claims"] == 1
    assert "claims_history" not in out["detail"]["timings_ms"]
    assert sidecar.CLAIMS_HEADER in out["text"] and "previously" not in out["text"]
    assert "history" not in out["detail"]["status"]
    # The flag forces the path on a present turn, and turns it off on a past one.
    on = app.prefetch({"query": "Where does Platypus live?", "dry_run": True, "include_history": True})
    assert calls == [1] and on["history"] == {"asked": "flag", "items": 2}
    assert on["detail"]["status"]["history"] == "flag" and "claims_history" in on["detail"]["timings_ms"]
    off = app.prefetch({"query": "Where did Platypus live before?", "dry_run": True, "include_history": False})
    assert calls == [1] and "history" not in off and off["claims"] == 1
    with pytest.raises(sidecar.SidecarError) as exc:
        app.prefetch({"query": "x y z", "include_history": "yes"})
    assert exc.value.code == "invalid_include_history"


def test_past_values_share_the_gate_and_the_claims_cap(tmp_path):
    # The gate drops a claim: its past values never come in alone.
    gated = sidecar.Quoth(ledger_path=tmp_path / "g.db", gate=relevance.Gate(claims_min_lexical=0.9))
    try:
        with gated.lock:
            _add(gated.conn, "Platypus lives in Quendorf.", key="city", now=T[0], kind="identity")
            _add(gated.conn, "Platypus lives in Brask.", key="city", now=T[1], kind="identity")
        out = gated.prefetch({"query": "Where did Platypus live before Brask and Quendorf with Orla?",
                              "dry_run": True})
        assert out["claims"] == 0 and "previously" not in out["text"]
        assert all(not c["passed"] for c in out["detail"]["candidates"]["claims"])
    finally:
        gated.close()
    # Many claims with past values: the cap holds and each current comes before its past.
    app = sidecar.Quoth(ledger_path=tmp_path / "c.db", gate=relevance.Gate())
    try:
        with app.lock:
            for n in range(5):
                for step in range(3):
                    _add(app.conn, f"Platypus tool {n} is version {step}.", key=f"tool{n}", now=T[step],
                         kind="environment")
        out = app.prefetch({"query": "Which Platypus tool versions did I use before?", "dry_run": True})
        claims = out["detail"]["claims"]
        assert out["claims"] == len(claims) == sidecar.PREFETCH_CLAIMS
        seen = set()
        for c in claims:
            if c.get("history_of"):
                assert c["history_of"] in seen
            else:
                seen.add(c["id"])
        assert len(seen) < sidecar.PREFETCH_CLAIMS  # past values took slots, the cap is unchanged
    finally:
        app.close()


def test_recall_view_shows_past_values(app):
    old, mid, new = _city_chain(app)
    text = sidecar.render_view(app, "recall where did Platypus live before")
    assert f"  #{mid} previously (from 2026-03-01 until 2026-05-01): Platypus lives in Brask." in text


# ------------------------------------------------------------------ eval follow modes

@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "ledger.db"
    c = core.open_ledger(path)
    ids = {"quendorf": _add(c, "Lives in Quendorf.", key="city", now=T[0], kind="identity")}
    ids["quendorf2"] = core.edit(c, ids["quendorf"], text="Lives in Quendorf town.", actor="t", now=T[1])["id"]
    ids["brask"] = _add(c, "Lives in Brask.", key="city", now=T[2], kind="identity")
    ids["zanzibar"] = _add(c, "Lives in Zanzibar.", key="city", now=T[3], kind="identity")
    ids["pip"] = _add(c, "Has a dog called Pip.", now=T[0], kind="identity")
    core.forget(c, ids["pip"], reason="x", actor="t", now=T[1])
    c.close()
    return path, ids


def test_follow_modes_resolve_current_past_or_exact(ledger):
    path, ids = ledger
    r = Resolver.open(path)
    try:
        assert r.resolve({"text": "Lives in Quendorf."})["ids"] == [ids["zanzibar"]]
        got = r.resolve({"text": "Lives in Quendorf.", "follow": "edits"})
        assert got == {"ids": [ids["quendorf2"]], "how": "text", "steps": {"edit": 1}, "historical": True}
        assert r.resolve({"text": "Lives in Brask.", "follow": "none"})["historical"] is True
        assert r.resolve({"text": "Lives in Zanzibar.", "follow": "edits"}) == {
            "ids": [ids["zanzibar"]], "how": "text", "steps": {}}
        # An old wording is never served; a forgotten value is no past value.
        assert r.resolve({"text": "Lives in Quendorf.", "follow": "none"}) == {"ids": [], "reason": "edited"}
        assert r.resolve({"text": "Has a dog called Pip.", "follow": "edits"})["reason"] == "forgotten"
        # The slot finds only what holds it now.
        assert r.resolve({"text": "gone", "subject_key": "city", "follow": "edits"})["reason"] == "not_found"
        # An explicit mode overrides the anchor's; False is a stale lookup.
        assert r.resolve({"text": "Lives in Brask.", "follow": "edits"}, follow="current")["ids"] == [ids["zanzibar"]]
        assert r.resolve({"text": "Lives in Quendorf."}, follow=False)["ids"] == [ids["quendorf"]]
    finally:
        r.close()


def _row(rid, sources, **expect):
    return {"id": rid, "question": "Where did I live before?", "lang": "en", "kind": "knowledge_update",
            "origin": "synthetic", "expect": {"abstain": False, "sources": sources, **expect}}


def _claim(text, **kw):
    return {"store": "claims", "match": {"claim": {"text": text, **kw}}}


def test_follow_validates_and_reports_historical(ledger):
    path, ids = ledger
    assert row_errors(_row("a", [_claim("x", follow="edits")])) == []
    assert row_errors(_row("a", [_claim("x", follow="forward")]))
    rows = [_row("past", [_claim("Lives in Brask.", follow="edits"), _claim("Lives in Brask.")], claims_judged=True)]
    r = Resolver.open(path)
    try:
        out, report = resolve_rows(rows, r)
    finally:
        r.close()
    assert out["past"]["expect"]["sources"] == [{"store": "claims", "match": {"locator": f"claim:{ids['brask']}"}},
                                               {"store": "claims", "match": {"locator": f"claim:{ids['zanzibar']}"}}]
    assert report["historical"] == 1 and report["resolved"] == 2


def test_a_past_value_matching_gold_is_a_hit_and_sweep_replays_it():
    row = {"id": "h", "kind": "knowledge_update", "origin": "synthetic", "lang": "en", "question": "q",
           "expect": {"abstain": False, "claims_judged": True,
                      "sources": [{"store": "claims", "match": {"locator": "claim:7"}}]}}
    items = [{"id": 8, "text": "now", "injected": True, "pool": True, "via": "lexical", "lexical": 1.0},
             {"id": 7, "text": "before", "injected": True, "pool": True, "via": "history", "history_of": 8,
              "lexical": 1.0}]
    obs = M.observe(row, {"claims": items})
    assert obs["block_rank"] == 2 and obs["stores"]["claims"]["injected_matched"] == 1
    assert obs["stores"]["claims"]["history_injected"] == 1
    block = M.block([obs])
    assert block["injected_hit_rate"] == 1.0 and block["stores"]["claims"]["history_rate"] == 1.0
    rec = SW.record(row, {"claims": items}, row["expect"]["sources"])
    assert rec["stores"]["claims"]["items"][1]["history_of"] == 0
    replay = SW.observe("h", rec, relevance.Gate())
    assert replay["block_rank"] == 2 and replay["stores"]["claims"]["injected"] == 2
    # A gate that drops the current claim drops its past value with it.
    strict = SW.observe("h", {**rec, "stores": {**rec["stores"], "claims": {**rec["stores"]["claims"], "items": [
        {**rec["stores"]["claims"]["items"][0], "lexical": 0.1}, rec["stores"]["claims"]["items"][1]]}}},
        relevance.Gate(claims_min_lexical=0.5))
    assert strict["stores"]["claims"]["injected"] == 0


def test_label_claims_offers_past_values_for_past_questions(tmp_path, ledger):
    path, ids = ledger
    rows = [{"id": "q1", "question": "Where did I live before Zanzibar?", "lang": "en", "kind": "fact",
             "origin": "real_user", "expect": {"abstain": False, "sources": [{"store": "vault", "match": {"path": "a.md"}}]}},
            {"id": "q2", "question": "Where do I live in Zanzibar?", "lang": "en", "kind": "fact",
             "origin": "real_user", "expect": {"abstain": False, "sources": [{"store": "vault", "match": {"path": "b.md"}}]}}]
    write_set(rows, tmp_path / "set.jsonl", privacy="private", fixture="private")
    out = tmp_path / "labelled.jsonl"
    summary = propose(tmp_path / "set.jsonl", path, out, model=None)
    assert summary["past_intent"] == 1 and summary["past_values"] == 2
    labelled = {json.loads(line)["id"]: json.loads(line) for line in out.read_text().splitlines()}
    past = labelled["q1"]["meta"]["claims_label"]
    assert past["past_intent"] is True and any(c.get("historical") for c in past["candidates"])
    pick = next(c for c in past["candidates"] if c.get("historical"))
    assert anchor(pick)["follow"] == "edits"
    present = labelled["q2"]["meta"]["claims_label"]
    assert "past_intent" not in present and not any(c.get("historical") for c in present["candidates"])
    assert "follow" not in anchor(present["candidates"][0])
