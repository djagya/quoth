"""`refresh-nightly --suite`: the nightly gate on an eval suite (synthetic data only).

Unit tests cover the decision pieces (tolerances, excused lost gold, per-role reasons, the
fixture check, retention of suite runs); `nightly.run` is then driven with a fake suite
evaluator, and end to end on the public fixture vault with a 1024-dim hash embedder standing
in for bge-m3 (as the gate parity tests do).
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys

import pytest

from quoth import nightly
from quoth.evals import load_set, write_set
from quoth.evals.compare import DEFAULT_TOLERANCES, compare
from test_nightly import setup
from test_refresh import FakeEmbedder

REPO = Path(__file__).resolve().parents[1]
PUBLIC = REPO / 'eval' / 'public'


def suite_file(tmp_path, sets, fixture='private', name='suite.json', **extra):
    path = tmp_path / name
    path.write_text(json.dumps({'schema': 'quoth-evalsuite/v1', 'name': 'nightly-test',
                                'fixture': fixture, 'sets': sets, **extra}))
    return path


NO_MEMORY = [{'id': 'nm', 'builtin': 'no-memory', 'role': 'regression'},
             {'id': 'nmq', 'builtin': 'no-memory', 'role': 'quality'},
             {'id': 'dev', 'builtin': 'no-memory', 'role': 'tune', 'split': 'dev'}]


# ------------------------------------------------------------------ decision pieces

def test_suite_tolerances_drop_latency_and_widen_only_in_content_mode():
    code = nightly.suite_tolerances(DEFAULT_TOLERANCES)
    assert not any(k.startswith('latency_ms.') for k in code['metrics'])
    assert code['metrics']['metrics.overall.stores.vault.hit@10'] == {'max_drop': 0.0}
    assert 'excused_lost_gold' not in code
    content = nightly.suite_tolerances(DEFAULT_TOLERANCES, mode='content', excused={'q2', 'q1'})
    assert content['metrics']['metrics.overall.stores.vault.hit@10'] == {'max_drop': nightly.CONTENT_TOLERANCE}
    # A looser rule stays as written; rise rules are untouched.
    assert content['metrics']['metrics.overall.injected_precision'] == {'max_drop': 0.05}
    assert content['metrics']['metrics.overall.stale_leak'] == {'max_rise': 0}
    assert content['excused_lost_gold'] == ['q1', 'q2']
    assert DEFAULT_TOLERANCES['metrics']['metrics.overall.stores.vault.hit@10'] == {'max_drop': 0.0}


def _receipt(hits):
    return {'set': {'sha256': 's', 'split': None}, 'status': 'ok', 'metrics': {'overall': {}},
            'items': {'vault_hit@10': hits}}


def test_compare_excuses_listed_lost_gold_only():
    a, b = _receipt({'q1': True, 'q2': True}), _receipt({'q1': False, 'q2': False})
    tol = {**DEFAULT_TOLERANCES, 'metrics': {}}
    assert compare(a, b, tol)['exit'] == 1
    half = compare(a, b, {**tol, 'excused_lost_gold': ['q1']})
    assert half['exit'] == 1 and half['excused_lost_gold'] == ['q1']
    full = compare(a, b, {**tol, 'excused_lost_gold': ['q1', 'q2', 'other']})
    assert full['exit'] == 0 and full['lost_gold'] == ['q1', 'q2']
    assert full['excused_lost_gold'] == ['q1', 'q2']


def test_excusable_names_rows_whose_gold_note_changed():
    rows = [{'id': 'a', 'expect': {'sources': [{'store': 'vault', 'match': {'path': 'x.md'}}]}},
            {'id': 'b', 'expect': {'sources': [{'store': 'vault', 'match': {'path': 'x.md'}},
                                               {'store': 'vault', 'match': {'path': 'y.md'}}]}},
            {'id': 'c', 'expect': {'sources': [{'store': 'claims', 'match': {'subject_key': 'k'}}]}}]
    changes = {'x.md': nightly.GOLD_NOTE_CHANGED, 'y.md': nightly.GOLD_NOTE_REMOVED}
    assert nightly.excusable(rows, changes) == {'a': 'gold_note_changed', 'b': 'gold_note_removed'}


def _comparison(**sets):
    out = {'schema': 'quoth-evalsuite-compare/v1', 'problems': [], 'sets': {}}
    for sid, (role, code) in sets.items():
        out['sets'][sid] = {'role': role, 'exit': code, 'result': {0: 'pass', 1: 'fail', 3: 'incomparable'}[code],
                            'excused_reasons': {'e1': 'gold_note_changed'},
                            'compare': {'lost_gold': ['l1', 'e1'], 'excused_lost_gold': ['e1'],
                                        'checks': [{'metric': 'm.ok', 'result': 'pass'},
                                                   {'metric': 'm.bad', 'result': 'fail' if code else 'pass'}],
                                        'paired': {'vault_hit@10': {'p': 0.01, 'significant': True,
                                                                    'direction': 'worse'}},
                                        'quality_failed_on': ['vault_hit@10'] if role == 'quality' and code else []}}
    codes = [code for _, code in sets.values()]
    out['exit'] = 3 if 3 in codes else 1 if 1 in codes else 0
    out['result'] = {0: 'pass', 1: 'fail', 3: 'incomparable'}[out['exit']]
    return out


def test_suite_gate_reasons_by_role():
    reasons, lost, excused, report = nightly.suite_gate(_comparison(
        guard=('regression', 1), smoke=('smoke', 0), q=('quality', 1)))
    assert reasons == ['suite_guard_regressed', 'suite_q_quality_worse']
    assert lost['guard'] == ['l1', 'e1']
    assert excused['guard'] == [{'id': 'e1', 'reason': 'gold_note_changed'}]
    assert report['guard']['failed_checks'] == ['m.bad'] and report['smoke']['failed_checks'] == []
    assert report['q']['quality_failed_on'] == ['vault_hit@10']
    assert report['q']['paired']['vault_hit@10']['direction'] == 'worse'
    assert nightly.suite_gate(_comparison(guard=('regression', 0)))[0] == []
    with pytest.raises(nightly.SuiteGateError, match='suite_incomparable'):
        nightly.suite_gate(_comparison(guard=('regression', 3)))
    broken = _comparison(guard=('regression', 0))
    broken['problems'] = ['guard: missing in B']
    with pytest.raises(nightly.SuiteGateError, match='suite_incomparable'):
        nightly.suite_gate(broken)


def test_gate_suite_checks_the_fixture_and_skips_tune_sets(tmp_path):
    gated, skipped = nightly.gate_suite(suite_file(tmp_path, NO_MEMORY))
    assert [e.id for e in gated.entries] == ['nm', 'nmq'] and skipped == ['dev']
    # The public suite targets the shipped fixture, never a real vault, unless named.
    public = REPO / 'eval' / 'suites' / 'public.json'
    with pytest.raises(nightly.SuiteGateError, match='suite_invalid'):
        nightly.gate_suite(public)
    assert [e.id for e in nightly.gate_suite(public, 'eval/public')[0].entries] == ['fixture', 'no-memory']
    # A set written for other stores is refused even when the suite's fixture matches.
    other = suite_file(tmp_path, [{'id': 'f', 'path': str(PUBLIC / 'set.jsonl'), 'role': 'smoke'}], name='o.json')
    with pytest.raises(nightly.SuiteGateError):
        nightly.gate_suite(other)
    only_tune = suite_file(tmp_path, NO_MEMORY[2:], name='t.json')
    with pytest.raises(nightly.SuiteGateError):
        nightly.gate_suite(only_tune)
    with pytest.raises(nightly.SuiteGateError):
        nightly.gate_suite(tmp_path / 'missing.json')


def test_prune_keeps_the_newest_suite_runs_and_nothing_foreign(tmp_path):
    root = tmp_path / nightly.SUITE_RUNS
    names = [f'2026010{d}T0000{s:02d}Z-{"a" * 12}' for d in range(1, 4) for s in range(6)]
    for name in names:
        (root / name).mkdir(parents=True)
    (root / 'keep-me').mkdir()
    (root / 'not-a-run.txt').write_text('x')
    removed = nightly.prune_suite_runs(tmp_path, keep=14)
    assert removed == sorted(names)[:4]
    assert sorted(p.name for p in root.iterdir()) == sorted([*sorted(names)[4:], 'keep-me', 'not-a-run.txt'])


# ------------------------------------------------------------------ nightly.run, fake evaluator

def run_suite_gate(tmp_path, comparison=None, suite=None, **kwargs):
    src, vault, pointer = setup(tmp_path)
    calls = []

    def evaluate(old, candidate, gated, out_dir, *, mode, note_changes):
        calls.append({'sets': [e.id for e in gated.entries], 'out_dir': out_dir, 'mode': mode})
        out_dir.mkdir(parents=True)
        return comparison or _comparison(nm=('regression', 0), nmq=('quality', 0)), {
            'gold_drift': {'renamed': 0, 'stale': 0, 'ids': []},
            'headlines': {'current': {'nm': {'scored': 3, 'ci95': {}}},
                          'candidate': {'nm': {'scored': 3, 'ci95': {}}}},
            'engine': 'quoth.evals.suite'}
    args = dict(pointer=pointer, vault=vault, staging=tmp_path / 'staging', receipts=tmp_path / 'receipts',
                suite=suite or suite_file(tmp_path, NO_MEMORY), embedder=FakeEmbedder(),
                suite_evaluate=evaluate, self_check=lambda *a: {'synthetic_fixture': True})
    args.update(kwargs)
    return nightly.run(**args), src, pointer, calls


def test_suite_gate_switches_and_reports_the_suite(tmp_path):
    receipt, src, pointer, calls = run_suite_gate(tmp_path)
    assert receipt['outcome'] == 'SWITCH' and pointer.resolve() == Path(receipt['candidate'])
    assert calls[0]['sets'] == ['nm', 'nmq']
    assert calls[0]['out_dir'] == (tmp_path / 'receipts' / 'suite-runs' / receipt['receipt_id'])
    saved = json.loads(Path(receipt['receipt']).read_text())
    assert saved['gate']['version'] == 'quoth-nightly-suite/v1' and saved['gate']['mode'] == saved['gate_mode']
    assert saved['suite']['roles'] == {'nm': 'regression', 'nmq': 'quality'}
    assert saved['suite']['skipped'] == ['dev'] and saved['suite']['result'] == 'pass'
    assert saved['suite']['runs'] == 'suite-runs/' + receipt['receipt_id']
    assert saved['suite']['sets']['nm']['current'] == {'scored': 3, 'ci95': {}}
    assert saved['serving_quality'] == {'status': 'not_measured', 'reason': 'suite_gate'}
    assert 'deprecated' not in saved and 'current_metrics' not in saved
    assert (tmp_path / 'receipts' / 'suite-runs').stat().st_mode & 0o777 == 0o700


def test_suite_gate_holds_on_a_failing_regression_set(tmp_path):
    receipt, src, pointer, _ = run_suite_gate(
        tmp_path, comparison=_comparison(nm=('regression', 1), nmq=('quality', 0)))
    assert receipt['outcome'] == 'HOLD' and pointer.resolve() == src
    assert receipt['reasons'] == ['suite_nm_regressed']
    assert receipt['lost_gold_ids']['nm'] == ['l1', 'e1']
    assert Path(receipt['candidate']).exists()


def test_suite_gate_fails_when_not_comparable_or_invalid(tmp_path):
    receipt, src, pointer, _ = run_suite_gate(tmp_path, comparison=_comparison(nm=('regression', 3)))
    assert receipt['outcome'] == 'FAIL' and receipt['reasons'] == ['suite_incomparable']
    assert receipt['suite']['result'] == 'incomparable' and pointer.resolve() == src


def test_an_invalid_suite_fails_before_anything_is_built(tmp_path):
    bad = suite_file(tmp_path, NO_MEMORY, fixture='eval/public')
    receipt, src, pointer, calls = run_suite_gate(tmp_path, suite=bad)
    assert receipt['outcome'] == 'FAIL' and receipt['reasons'] == ['suite_invalid']
    assert not calls and 'candidate' not in receipt and pointer.resolve() == src


def test_suite_gate_records_excessive_drift_as_the_legacy_gate_does(tmp_path):
    from quoth.gold_drift import ExcessiveDrift

    def evaluate(*args, **kwargs):
        raise ExcessiveDrift({'renamed': 0, 'stale': 2, 'ids': ['a', 'b']})
    receipt, src, pointer, _ = run_suite_gate(tmp_path, suite_evaluate=evaluate)
    assert receipt['outcome'] == 'FAIL' and receipt['reasons'] == ['gold_drift_excessive']
    assert receipt['gold_drift'] == {'renamed': 0, 'stale': 2, 'ids': ['a', 'b']}


# ------------------------------------------------------------------ CLI

def _cli(tmp_path, *extra):
    return ['refresh-nightly', '--pointer', str(tmp_path / 'p'), '--vault', str(tmp_path),
            '--staging', str(tmp_path / 's'), '--receipts', str(tmp_path / 'r'), *extra]


@pytest.mark.parametrize('extra', [
    ('--suite', 's.json', '--gate-set', 'g.jsonl'),
    ('--suite', 's.json', '--private-runner', 'r.py'),
    ('--suite', 's.json', '--quality-set', 'q.jsonl'),
    ('--suite-fixture', 'private', '--gate-set', 'g.jsonl', '--private-runner', 'r.py'),
    ('--gate-set', 'g.jsonl'),
    (),
])
def test_suite_and_legacy_flags_are_exclusive(tmp_path, capsys, extra):
    from quoth.cli import main
    with pytest.raises(SystemExit) as exc:
        main(_cli(tmp_path, *extra))
    assert exc.value.code == 2 and '--' in capsys.readouterr().err


def test_cli_suite_run_records_the_reason_and_stays_quiet_on_stderr(tmp_path, capsys):
    from quoth.cli import main
    pointer = tmp_path / 'p'
    pointer.symlink_to(tmp_path / 'missing.db')
    assert main(_cli(tmp_path, '--suite', str(tmp_path / 'missing-suite.json'))) == 1
    out, err = capsys.readouterr()
    assert not err and out.startswith('FAIL quoth-nightly receipt=')
    saved = json.loads(Path(out.splitlines()[0].split('receipt=', 1)[1]).read_text())
    assert saved['reasons'] == ['suite_invalid'] and 'deprecated' not in saved


def test_private_runner_deprecation_is_printed_at_a_terminal_only(tmp_path, capsys, monkeypatch):
    from quoth.cli import main
    pointer = tmp_path / 'p'
    pointer.symlink_to(tmp_path / 'missing.db')
    legacy = _cli(tmp_path, '--gate-set', str(tmp_path / 'g.jsonl'), '--private-runner', str(tmp_path / 'r.py'))
    monkeypatch.setattr(sys.stderr, 'isatty', lambda: True)
    assert main(legacy) == 1
    err = capsys.readouterr().err
    assert err == nightly.DEPRECATED_RUNNER + '\n' and str(tmp_path) not in err


# ------------------------------------------------------------------ end to end, public fixture

@pytest.fixture(scope='module')
def base(tmp_path_factory):
    from test_gate_parity import frozen_snapshot
    root = tmp_path_factory.mktemp('suite-base')
    shutil.copytree(PUBLIC / 'vault', root / 'vault')
    return frozen_snapshot(root / 'vault', root / 'current.db'), root / 'vault'


def _served(base, tmp_path):
    current, base_vault = base
    vault = tmp_path / 'vault'
    shutil.copytree(base_vault, vault)
    work = tmp_path / 'current.db'
    shutil.copy(current, work)
    manifest = json.loads(current.with_suffix('.manifest.json').read_text())
    manifest.update(db=str(work), vault_root=str(vault))
    work.with_suffix('.manifest.json').write_text(json.dumps(manifest))
    pointer = tmp_path / 'serving.db'
    pointer.symlink_to(work)
    return work, vault, pointer


def _public_suite(tmp_path):
    from test_gate_parity import CROSS_SCRIPT
    rows = [r for r in load_set(PUBLIC / 'set.jsonl').rows
            if r['id'] not in CROSS_SCRIPT
            and (r['expect']['abstain'] or any(s['store'] == 'vault' for s in r['expect']['sources']))]
    write_set(rows, tmp_path / 'sets' / 'guard.jsonl', privacy='public', fixture='eval/public')
    return suite_file(tmp_path, [{'id': 'guard', 'path': 'sets/guard.jsonl', 'role': 'regression'},
                                 {'id': 'tune', 'path': 'sets/guard.jsonl', 'role': 'tune'}],
                      fixture='eval/public'), rows


@pytest.mark.parametrize('scenario,outcome,reasons', [
    ('switch', 'SWITCH', []),
    ('lost_gold', 'HOLD', ['suite_guard_regressed']),
    ('drift_excessive', 'FAIL', ['gold_drift_excessive']),
])
def test_suite_gate_end_to_end_on_the_public_fixture(scenario, outcome, reasons, base, tmp_path, monkeypatch):
    from quoth import recall_defaults
    from test_gate_parity import EMB, SCENARIOS
    # The scenarios' expected decisions were set on dense ranking (see test_gate_parity).
    monkeypatch.setattr(recall_defaults, 'RETRIEVAL', 'dense')
    monkeypatch.setattr(nightly, 'get_embedder', lambda *a, **kw: EMB)
    work, vault, pointer = _served(base, tmp_path)
    SCENARIOS[scenario][0](vault)
    suite, rows = _public_suite(tmp_path)
    receipts = tmp_path / 'receipts'
    receipt = nightly.run(pointer, vault, tmp_path / 'staging', receipts, suite=suite,
                          suite_fixture='eval/public', embedder=EMB,
                          self_check=lambda *a: {'synthetic_fixture': True})
    assert receipt['outcome'] == outcome and receipt['reasons'] == reasons
    assert pointer.resolve() == (Path(receipt['candidate']) if outcome == 'SWITCH' else work)
    text = Path(receipt['receipt']).read_text()
    runs = receipts / 'suite-runs' / receipt['receipt_id']
    blobs = [text, *(p.read_text() for p in runs.rglob('*.json'))]
    for row in rows:
        assert all(row['question'] not in blob for blob in blobs)
    if outcome == 'FAIL':
        assert receipt['gold_drift']['stale'] > 0
        return
    saved = json.loads(text)
    assert set(saved['suite']['sets']) == {'guard'} and saved['suite']['skipped'] == ['tune']
    assert (runs / 'current' / 'guard.receipt.json').is_file() and (runs / 'compare.json').is_file()
    assert not (runs / 'current' / 'tune.receipt.json').exists()
    guard = saved['suite']['sets']['guard']
    assert guard['current']['scored'] == guard['candidate']['scored'] > 0
    assert 'ci95' in guard['candidate'] and 'vault_hit@10' in guard['paired']
    if outcome == 'HOLD':
        assert saved['lost_gold_ids']['guard']
        assert 'metrics.overall.stores.vault.hit@10' in guard['failed_checks']
    # The same suite, run again by `eval suite compare`, agrees with the receipt.
    from quoth.evals.suite import compare_suites
    again = compare_suites(runs / 'current', runs / 'candidate', nightly.gate_suite(suite, 'eval/public')[0],
                           tolerances_for=lambda sid, tol: nightly.suite_tolerances(tol, mode=saved['gate_mode']))
    assert again['result'] == ('pass' if outcome == 'SWITCH' else 'fail')


def test_a_quality_set_does_not_hold_on_an_insignificant_loss(base, tmp_path, monkeypatch):
    from quoth import recall_defaults
    from test_gate_parity import EMB, SCENARIOS
    monkeypatch.setattr(recall_defaults, 'RETRIEVAL', 'dense')
    monkeypatch.setattr(nightly, 'get_embedder', lambda *a, **kw: EMB)
    work, vault, pointer = _served(base, tmp_path)
    SCENARIOS['lost_gold'][0](vault)
    _public_suite(tmp_path)
    suite = suite_file(tmp_path, [{'id': 'q', 'path': 'sets/guard.jsonl', 'role': 'quality'}],
                       fixture='eval/public', name='quality.json')
    receipt = nightly.run(pointer, vault, tmp_path / 'staging', tmp_path / 'receipts', suite=suite,
                          suite_fixture='eval/public', embedder=EMB,
                          self_check=lambda *a: {'synthetic_fixture': True})
    # One lost row out of a dozen is no significant paired change: the quality set reports it.
    assert receipt['outcome'] == 'SWITCH' and receipt['reasons'] == []
    q = receipt['suite']['sets']['q']
    assert q['role'] == 'quality' and q['result'] == 'pass' and receipt['lost_gold_ids']['q']
    assert q['paired']['vault_hit@10']['significant'] is False
