"""`eval convert --from private-v2`: the private gate runner's rows as an eval set v2.

Synthetic rows only: the runner's format is `{id, question, expected_paths}` plus optional
`kind`, `lang`, `origin`, `asked_at` (docs/FRESHNESS.md#the-private-gate-runner).
"""
from __future__ import annotations

import json

import pytest

from quoth import nightly
from quoth.evals import load_set
from quoth.evals.cli import main as eval_main
from quoth.evals.convert import ConvertError, convert, from_private_v2
from quoth.evals.gate import gate_rows

ROWS = [
    {'id': 'p1', 'question': 'Which bell rings twice?', 'expected_paths': ['MISC/Bell.md'], 'kind': 'ordinary'},
    {'id': 'p2', 'question': 'Wo steht das Fahrrad?', 'expected_paths': ['HOME/Bike.md', 'HOME/Shed.md'],
     'lang': 'de', 'asked_at': '2026-02-01', 'origin': 'assistant-query', 'scope': 'home'},
    {'id': 'p3', 'question': 'Who won the 1931 lantern race?', 'expected_paths': ['X/Decoy.md'],
     'kind': 'adversarial'},
    {'id': 'p4', 'question': 'Hello there', 'expected_paths': []},
]


def write_rows(path, rows=ROWS):
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows), encoding='utf-8')
    return path


def test_private_v2_rows_become_reviewed_real_user_rows():
    out = {r['id']: r for r in from_private_v2(ROWS)}
    p1 = out['p1']
    assert p1['origin'] == 'real_user' and p1['provenance'] == {'author': 'owner'}
    assert p1['kind'] == 'fact' and p1['tags'] == ['legacy_kind:ordinary'] and p1['lang'] == 'und'
    assert p1['expect'] == {'abstain': False, 'sources': [{'store': 'vault', 'match': {'path': 'MISC/Bell.md'}}]}
    # A row's own origin wins; unknown keys are kept under meta.
    p2 = out['p2']
    assert p2['origin'] == 'assistant_query' and p2['lang'] == 'de' and p2['asked_at'] == '2026-02-01'
    assert p2['meta'] == {'scope': 'home'} and len(p2['expect']['sources']) == 2
    # The runner never judged adversarial rows: they abstain, their paths kept out of the gold.
    p3 = out['p3']
    assert p3['kind'] == 'unanswerable' and p3['expect'] == {'abstain': True, 'sources': []}
    assert p3['meta'] == {'unjudged_expected_paths': ['X/Decoy.md']}
    assert out['p4']['expect']['abstain'] is True and out['p4']['kind'] == 'fact'
    assert from_private_v2(ROWS[:1], origin='authored')[0]['origin'] == 'authored'


def test_private_v2_rejects_other_shapes():
    with pytest.raises(ConvertError):
        from_private_v2([{'id': 'x', 'question': 'q'}])
    with pytest.raises(ConvertError):
        from_private_v2([{'id': 'x', 'question': 'q', 'expected_paths': 'a.md'}])
    with pytest.raises(ConvertError):
        from_private_v2([{**ROWS[0], 'origin': 'somebody'}])


def test_convert_cli_writes_a_private_v2_set_the_suite_gate_accepts(tmp_path, capsys):
    src = write_rows(tmp_path / 'real-questions-v2.jsonl')
    out = tmp_path / 'real-questions.evalset.jsonl'
    assert eval_main(['convert', '--from', 'private-v2', str(src), str(out)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary['rows'] == 4 and summary['counts']['origin'] == {'assistant_query': 1, 'real_user': 3}
    assert 'Which bell' not in json.dumps(summary)
    evalset = load_set(out)
    assert evalset.manifest['schema'] == 'quoth-evalset/v2' and evalset.manifest['privacy'] == 'private'
    assert evalset.manifest['fixture'] == 'private' == nightly.SUITE_FIXTURE
    assert evalset.manifest['source']['format'] == 'private-v2'
    assert out.stat().st_mode & 0o777 == 0o600
    # The gate sees the same rows the runner judged: p1 and p2 with their paths.
    judged = {r['id']: r['expected_paths'] for r in gate_rows(evalset) if r['expected_paths']}
    assert judged == {'p1': ['MISC/Bell.md'], 'p2': ['HOME/Bike.md', 'HOME/Shed.md']}
    suite = tmp_path / 'suite.json'
    suite.write_text(json.dumps({'schema': 'quoth-evalsuite/v1', 'name': 'personal', 'fixture': 'private',
                                 'sets': [{'id': 'real', 'path': out.name, 'role': 'regression'},
                                          {'id': 'nm', 'builtin': 'no-memory', 'role': 'regression'}]}))
    gated, skipped = nightly.gate_suite(suite)
    assert [e.id for e in gated.entries] == ['real', 'nm'] and skipped == []


def test_convert_rejects_bad_rows_without_printing_them(tmp_path, capsys):
    src = write_rows(tmp_path / 'bad.jsonl', [{'id': 'bad id', 'question': 'SECRET QUESTION', 'expected_paths': []}])
    assert eval_main(['convert', '--from', 'private-v2', str(src), str(tmp_path / 'o.jsonl')]) == 2
    captured = capsys.readouterr()
    assert 'SECRET QUESTION' not in captured.out + captured.err
    assert not (tmp_path / 'o.jsonl').exists()
    with pytest.raises(ConvertError):
        convert('private-v3', src, tmp_path / 'o.jsonl')
