"""Nightly safety/gate tests: synthetic DBs, no live vault/model/serving writes."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from quoth import nightly
from quoth.freshness import successor
from quoth.refresh import sha
from quoth.serving import ServingSnapshot, replace_pointer
from test_freshness import accepted
from test_refresh import FakeEmbedder


def scores(hit=True, recall=1.0, mrr=1.0):
    return {d: {'n': 1, 'hit@10': float(hit), 'mrr@10': mrr,
                'recall@10': recall, 'items': {'q1': hit}}
            for d in ('private', 'synthetic')}


def setup(tmp_path):
    src, vault = accepted(tmp_path)
    pointer = tmp_path / 'serving.db'
    pointer.symlink_to(src)
    (vault / 'a.md').write_text('# Changed\nPrivate sentinel fixture.')
    return src, vault, pointer


def run(tmp_path, evaluate=None, self_check=None, **kwargs):
    src, vault, pointer = setup(tmp_path)
    args = dict(pointer=pointer, vault=vault, staging=tmp_path / 'staging',
                receipts=tmp_path / 'receipts', gate_set=tmp_path / 'golden.jsonl',
                private_runner=tmp_path / 'runner.py', embedder=FakeEmbedder(),
                evaluate=evaluate or (lambda *a: (scores(), scores(), {'synthetic_fixture': True})),
                self_check=self_check or (lambda *a: {'synthetic_fixture': True}))
    args.update(kwargs)
    return nightly.run(**args), src, pointer


def test_gate_exact_threshold_and_zero_gold_guard():
    a, b = scores(), scores(recall=0.99)
    assert nightly.gate(a, b) == ([], {'private': [], 'synthetic': []},
                                  {'private': [], 'synthetic': []})
    b['synthetic']['recall@10'] = 0.989
    assert 'synthetic_recall@10_regressed' in nightly.gate(a, b)[0]
    b = scores(mrr=0.99999)
    assert nightly.gate(a, b)[0] == ['private_mrr@10_regressed']
    a, b = scores(False, 0.0, 0.0), scores(False, 0.0, 0.0)
    a['synthetic']['items'] = {'lost': True, 'gain': False}
    b['synthetic']['items'] = {'lost': False, 'gain': True}
    assert nightly.gate(a, b)[1]['synthetic'] == ['lost']


def test_gate_refuses_nan_and_different_sets():
    a, b = scores(), scores(mrr=float('nan'))
    with pytest.raises(ValueError, match='metric'):
        nightly.gate(a, b)
    b = scores()
    b['private']['items'] = {'other': True}
    with pytest.raises(ValueError, match='item_set'):
        nightly.gate(a, b)


def test_switch_receipt_modes_and_source_unchanged(tmp_path):
    receipt, src, pointer = run(tmp_path)
    assert receipt['outcome'] == 'SWITCH'
    assert pointer.resolve() == Path(receipt['candidate'])
    assert sha(src) == receipt['old']['snapshot_id']
    saved = Path(receipt['receipt'])
    assert saved.stat().st_mode & 0o777 == 0o600
    assert saved.parent.stat().st_mode & 0o777 == 0o700
    payload = json.loads(saved.read_text())
    assert payload['outcome'] == 'SWITCH'
    assert 'items' not in saved.read_text()
    assert 'Private sentinel' not in saved.read_text()


def test_hold_retains_candidate_and_pointer(tmp_path):
    r, src, pointer = run(tmp_path, evaluate=lambda *a: (scores(), scores(False, 0, 0), {}))
    assert r['outcome'] == 'HOLD'
    assert pointer.resolve() == src
    assert Path(r['candidate']).exists()
    assert r['lost_gold_ids'] == {'private': ['q1'], 'synthetic': ['q1']}


def test_failed_probe_rolls_back_no_retention(tmp_path):
    def fail(*a):
        raise RuntimeError('private sentinel')
    r, src, pointer = run(tmp_path, self_check=fail)
    assert r['outcome'] == 'FAIL'
    assert r['rollback'] == 'restored_previous'
    assert pointer.resolve() == src
    assert Path(r['candidate']).exists()
    assert r['reasons'] == ['self_check_failed']
    assert 'private sentinel' not in Path(r['receipt']).read_text()


def test_disk_shortage_and_failure_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(nightly.shutil, 'disk_usage', lambda *a: SimpleNamespace(free=0))
    r, src, pointer = run(tmp_path)
    assert r['outcome'] == 'FAIL'
    assert r['reasons'] == ['preflight_failed']
    assert pointer.resolve() == src
    assert 'candidate' not in r


def test_fresh_skip_and_lock(tmp_path):
    src, vault = accepted(tmp_path)
    pointer = tmp_path / 'serving.db'
    pointer.symlink_to(src)
    args = (pointer, vault, tmp_path / 'staging', tmp_path / 'receipts', tmp_path / 'golden', tmp_path / 'runner')
    assert nightly.run(*args)['outcome'] == 'SKIP'
    assert not (tmp_path / 'staging').exists()
    with pointer.with_name('serving.db.refresh.lock').open('w') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        assert nightly.run(*args)['outcome'] == 'BUSY'


@pytest.mark.parametrize('bad', ['digest', 'missing', 'manifest', 'regular'])
def test_pointer_invalid_change_retains_old(tmp_path, bad):
    src, vault, pointer = setup(tmp_path)
    serving = ServingSnapshot(pointer)
    candidate = tmp_path / 'candidate.db'
    successor(src, candidate, vault, embedder=FakeEmbedder())
    if bad == 'digest':
        candidate.chmod(0o600)
        with candidate.open('ab') as stream:
            stream.write(b'bad')
    elif bad == 'manifest':
        candidate.with_suffix('.manifest.json').chmod(0o600)
        candidate.with_suffix('.manifest.json').write_text('{}')
    replace_pointer(pointer, candidate)
    if bad == 'missing':
        candidate.unlink()
    if bad == 'regular':
        pointer.unlink()
        pointer.write_text('bad')
    with serving.lease() as (conn, identity):
        assert identity == sha(src)
        assert conn.execute('PRAGMA query_only').fetchone()[0] == 1
    serving.close()


def test_concurrent_call_during_swap_leases_old_until_finished(tmp_path):
    src, vault, pointer = setup(tmp_path)
    candidate = tmp_path / 'candidate.db'
    successor(src, candidate, vault, embedder=FakeEmbedder())
    serving = ServingSnapshot(pointer)
    ready, finish = threading.Event(), threading.Event()
    def in_flight():
        with serving.lease() as (conn, identity):
            ready.set()
            assert finish.wait(5)
            assert conn.execute('PRAGMA query_only').fetchone()[0] == 1
            return identity
    def next_call():
        with serving.lease() as (_, identity):
            return identity
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(in_flight)
        assert ready.wait(5)
        replace_pointer(pointer, candidate)
        second = pool.submit(next_call)
        assert not second.done()
        finish.set()
        assert first.result(5) == sha(src)
        assert second.result(5) == sha(candidate)
    serving.close()


def test_retention_preserves_f4_current_previous_last_hold_and_foreign(tmp_path):
    src, vault, pointer = setup(tmp_path)
    files = []
    for name in ['nightly-current', 'nightly-previous', 'nightly-held', 'nightly-old', 'f4-rollback', 'foreign']:
        candidate = tmp_path / (name + '.db')
        successor(src, candidate, vault, embedder=FakeEmbedder())
        files.append(candidate)
    replace_pointer(pointer, files[0])
    assert nightly.retention(tmp_path, pointer, files[1], str(files[2])) == ['nightly-old.db']
    assert all(p.exists() for p in files if p != files[3])
    assert not files[3].with_suffix('.manifest.json').exists()


def test_cli_help_exposes_command(capsys):
    from quoth.cli import main
    with pytest.raises(SystemExit) as exc:
        main(['refresh-nightly', '--help'])
    assert exc.value.code == 0
    assert '--pointer' in capsys.readouterr().out


def test_real_build_pair_excessive_deletion_fails_closed_with_synthetic_model(tmp_path, monkeypatch):
    import numpy as np
    src, vault, pointer = setup(tmp_path)
    (vault / 'deleted.md').unlink()
    from conftest import gate_set
    row = {'id': 'synthetic-1', 'question': 'old evidence', 'kind': 'ordinary',
           'expected_paths': ['deleted.md']}
    golden = gate_set(tmp_path / 'golden.jsonl', [row])
    private = tmp_path / 'real-questions-v2.jsonl'
    private.write_text(json.dumps({**row, 'id': 'private-fixture-1'}) + '\n')
    runner = tmp_path / 'fixture-runner.py'
    # Synthetic scorer interface; no private payload/code copied into fixtures.
    runner.write_text('''def validate(rows):
    assert rows and len({r['id'] for r in rows}) == len(rows)
def metrics(h5, h10, expected):
    ranks = [i for i, h in enumerate(h10, 1) if h['path'] in expected]
    return {'hit@10': int(bool(ranks)), 'mrr@10': 1 / ranks[0] if ranks else 0}
''')
    class QueryFake(FakeEmbedder):
        def embed_query(self, text):
            vector = np.zeros(1024, dtype='<f4')
            vector[0] = 1
            return vector
    monkeypatch.setattr(nightly, 'get_embedder', lambda *a, **kw: QueryFake())
    receipt = nightly.run(pointer, vault, tmp_path / 'staging', tmp_path / 'receipts',
                          golden, runner, embedder=QueryFake())
    assert receipt['outcome'] == 'FAIL'
    assert receipt['reasons'] == ['gold_drift_excessive']
    assert receipt['gold_drift'] == {'renamed': 0, 'stale': 2,
        'ids': ['private-fixture-1', 'synthetic-1']}
    assert 'current_metrics' not in receipt and 'candidate_metrics' not in receipt
    assert pointer.resolve() == src
    assert not (tmp_path / '__pycache__').exists()
    print('synthetic nightly dry-run:', json.dumps({
        'outcome': receipt['outcome'], 'seconds': receipt['seconds'],
        'gold_drift': receipt['gold_drift']}))


def test_external_pointer_change_during_eval_is_not_overwritten(tmp_path):
    def evaluate(old, candidate, *args):
        pointer = tmp_path / 'serving.db'
        replace_pointer(pointer, candidate)
        return scores(), scores(), {}
    r, src, pointer = run(tmp_path, evaluate=evaluate)
    assert r['outcome'] == 'FAIL'
    assert r['reasons'] == ['recheck_failed']
    assert pointer.resolve() != src


def test_vault_drift_during_eval_holds_pointer(tmp_path):
    def evaluate(*args):
        (tmp_path / 'vault' / 'a.md').write_text('# Drift\nFreshness changed during eval.')
        return scores(), scores(), {}
    r, src, pointer = run(tmp_path, evaluate=evaluate)
    assert r['outcome'] == 'FAIL'
    assert r['reasons'] == ['recheck_failed']
    assert pointer.resolve() == src


def test_cli_real_subprocess_fail_line_and_receipt(tmp_path):
    import subprocess
    import sys
    pointer = tmp_path / 'serving.db'
    pointer.symlink_to(tmp_path / 'missing.db')
    result = subprocess.run([sys.executable, '-m', 'quoth.cli', 'refresh-nightly',
        '--pointer', str(pointer), '--vault', str(tmp_path / 'vault'),
        '--staging', str(tmp_path / 'staging'), '--receipts', str(tmp_path / 'receipts'),
        '--gate-set', str(tmp_path / 'golden.jsonl'),
        '--private-runner', str(tmp_path / 'private-runner.py')],
        capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1],
        env={'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')})
    assert result.returncode == 1
    assert not result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith('FAIL quoth-nightly receipt=')
    receipt = Path(lines[0].split('receipt=', 1)[1])
    assert json.loads(receipt.read_text())['outcome'] == 'FAIL'
    assert lines[1] == ('RED quoth-heartbeat serving_pointer_invalid,last_run_fail,'
                        'freshness_unconfirmed')
    beat = json.loads((tmp_path / 'heartbeat.json').read_text())
    assert beat['status'] == 'red' and beat['last_run']['outcome'] == 'FAIL'


def test_golden_is_required(tmp_path, capsys):
    from quoth.cli import main
    # The packaged fixture set must never silently stand in for the install's private set.
    with pytest.raises(SystemExit) as exc:
        main(['refresh-nightly', '--pointer', str(tmp_path / 'p'), '--vault', str(tmp_path),
              '--staging', str(tmp_path), '--receipts', str(tmp_path),
              '--private-runner', str(tmp_path / 'r.py')])
    assert exc.value.code == 2 and '--gate-set' in capsys.readouterr().err
