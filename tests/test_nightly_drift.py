"""Real local CLI/build/evidence consumption with synthetic gold and vectors."""
import json
from pathlib import Path
import signal
import sqlite3
import time

import numpy as np
import pytest

from quoth import nightly, quality
from quoth.cli import main
from quoth.freshness import successor
from quoth.refresh import sha
from test_nightly import setup, scores
from test_refresh import FakeEmbedder
from conftest import gate_set


class QueryFake(FakeEmbedder):
    def embed_query(self, text):
        vector = np.zeros(1024, dtype='<f4')
        vector[0] = 1
        return vector


def corpus(tmp_path, paths):
    rows = [{'id': f'q{i}', 'question': f'SYNTHETIC SECRET question {i}', 'kind': 'ordinary',
             'expected_paths': paths(i)} for i in range(40)]
    golden = gate_set(tmp_path / 'golden.jsonl', rows)
    payload = ''.join(json.dumps(q) + '\n' for q in rows)
    (tmp_path / 'real-questions-v2.jsonl').write_text(payload)
    runner = tmp_path / 'runner.py'
    runner.write_text('''def validate(rows):
    assert 30 <= len(rows) <= 50
    assert len({q['id'] for q in rows}) == len(rows)
def metrics(h5, h10, expected):
    ranks = [i for i, h in enumerate(h10, 1) if h['path'] in expected]
    return {'hit@10': int(bool(ranks)), 'mrr@10': 1/ranks[0] if ranks else 0}
''')
    return golden, runner


def test_real_pair_symmetric_rename_and_deletion_then_daily_cache(tmp_path, monkeypatch):
    src, vault, pointer = setup(tmp_path)
    (vault / 'deleted.md').rename(vault / 'renamed.md')
    golden, runner = corpus(tmp_path, lambda i: ['a.md', 'deleted.md'])
    inputs = [golden, runner, tmp_path / 'real-questions-v2.jsonl']
    before = [sha(p) for p in [src, *inputs]]
    target = tmp_path / 'candidate.db'
    successor(src, target, vault, embedder=QueryFake())
    monkeypatch.setattr(nightly, 'get_embedder', lambda *a, **kw: QueryFake())
    monkeypatch.setattr(quality, 'get_embedder', lambda *a, **kw: QueryFake())
    a, b, evidence = nightly.evaluate_pair(src, target, golden, runner)
    assert nightly.gate(a, b)[0] == []
    assert a['private']['recall@10'] == b['private']['recall@10'] == 1
    # Once the renamed snapshot serves, frozen gold must still resolve next night
    # using its retained old identity, not turn into newly stale gold.
    served_a, served_b, served_evidence = nightly.evaluate_pair(target, target, golden, runner)
    assert served_evidence['gold_drift'] == evidence['gold_drift']
    assert nightly.gate(served_a, served_b)[0] == []
    assert evidence['gold_drift'] == {'renamed': 80, 'stale': 0, 'ids': [f'q{i}' for i in sorted(range(40), key=lambda i: f'q{i}')]}
    receipts = tmp_path / 'receipts'
    receipts.mkdir()
    first = quality.daily(target, sha(target), receipts, golden, runner)
    assert first['status'] == 'measured' and first['gold_drift']['renamed'] == 80
    assert first['metrics']['private']['recall@10'] == 1
    (receipts / 'first.json').write_text(json.dumps({'schema': 'quoth-nightly/v1', 'serving_quality': first}))
    assert quality.daily(target, sha(target), receipts, golden, runner)['status'] == 'cached'
    # Source deletion and updated synthetic gold: one entire item is removed from BOTH sides.
    golden, runner = corpus(tmp_path, lambda i: ['renamed.md'] if i == 0 else ['a.md'])
    (vault / 'renamed.md').unlink()
    changed = tmp_path / 'changed.db'
    successor(src, changed, vault, embedder=QueryFake())
    a, b, evidence = nightly.evaluate_pair(src, changed, golden, runner)
    assert evidence['gold_drift'] == {'renamed': 0, 'stale': 2, 'ids': ['q0']}
    for dataset in ('private', 'synthetic'):
        assert a[dataset]['n'] == b[dataset]['n'] == 39
        assert set(a[dataset]['items']) == set(b[dataset]['items'])
        assert 'q0' not in a[dataset]['items']
    assert nightly.gate(a, b)[0] == []
    second = quality.daily(target, sha(target), receipts, golden, runner)
    assert second['status'] == 'measured' and not second['cached']
    assert second['gold_drift']['stale'] == 2
    assert second['context_sha256'] != first['context_sha256']
    assert 'SYNTHETIC SECRET' not in json.dumps(second) and 'renamed.md' not in json.dumps(second)
    assert sha(src) == before[0] and pointer.resolve() == src
    assert not (tmp_path / '__pycache__').exists()


def test_excessive_drift_fail_receipt_and_daily_same_reason(tmp_path, monkeypatch):
    src, vault, pointer = setup(tmp_path)
    (vault / 'deleted.md').unlink()
    golden, runner = corpus(tmp_path, lambda i: ['deleted.md'] if i < 5 else ['a.md'])
    result = nightly.run(pointer, vault, tmp_path / 'stage', tmp_path / 'receipts',
                         golden, runner, embedder=QueryFake())
    assert result['outcome'] == 'FAIL' and result['reasons'] == ['gold_drift_excessive']
    saved = json.loads(Path(result['receipt']).read_text())
    assert saved['gold_drift'] == {'renamed': 0, 'stale': 10, 'ids': [f'q{i}' for i in range(5)]}
    assert saved['serving_quality']['reason'] == 'gold_drift_excessive'
    assert pointer.resolve() == src
    assert 'SYNTHETIC SECRET' not in json.dumps(saved) and 'deleted.md' not in json.dumps(saved)


def test_cli_deadline_receipt_checkpoint_committed_batch_and_resume(tmp_path, monkeypatch, capsys):
    src, vault, pointer = setup(tmp_path)
    (vault / 'new.md').write_text('# New\nSynthetic evidence.')
    calls = 0
    def slow_second():
        nonlocal calls
        calls += 1
        if calls == 2:
            time.sleep(2)
    embedder = QueryFake(slow_second)
    original = nightly.run
    def configured(*a, **kw):
        return original(*a, **kw, embedder=embedder, batch_size=1,
            evaluate=lambda *a: (scores(), scores(), {}),
            self_check=lambda *a: {'synthetic': True})
    monkeypatch.setattr(nightly, 'run', configured)
    staging, receipts = tmp_path / 'stage', tmp_path / 'receipts'
    args = ['refresh-nightly', '--pointer', str(pointer), '--vault', str(vault),
            '--staging', str(staging), '--receipts', str(receipts),
            '--gate-set', str(tmp_path / 'golden.jsonl'),
            '--private-runner', str(tmp_path / 'runner'), '--deadline-seconds', '0.5']
    handler = signal.getsignal(signal.SIGALRM)
    before = sha(src)
    # INCOMPLETE is non-zero so the scheduler cannot report a stuck refresh as ok.
    assert main(args) == 3
    line, red = capsys.readouterr().out.strip().splitlines()
    assert line.startswith('INCOMPLETE quoth-nightly receipt=')
    assert red.startswith('RED quoth-heartbeat ') and 'last_run_incomplete' in red
    cold = json.loads(Path(line.split('receipt=', 1)[1]).read_text())
    target = Path(cold['candidate'])
    assert cold['reasons'] == ['internal_deadline']
    assert cold['seconds']['total'] < 1.5
    assert cold['serving_quality'] == {'status': 'deferred', 'reason': 'internal_deadline'}
    assert target.with_suffix('.checkpoint.json').is_file()
    assert not target.with_suffix('.manifest.json').exists()
    assert nightly.resumable_target(staging, before) == target
    with sqlite3.connect(target) as conn:
        committed = conn.execute('SELECT chunk_id, vector FROM embeddings ORDER BY chunk_id').fetchall()
    assert pointer.resolve() == src and sha(src) == before
    assert signal.getsignal(signal.SIGALRM) == handler
    assert signal.getitimer(signal.ITIMER_REAL) == (0, 0)
    embedder = QueryFake()
    assert main(args[:-2]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith('SWITCH quoth-nightly receipt=')
    resumed = json.loads(Path(line.split('receipt=', 1)[1]).read_text())
    assert resumed['resumed_from'] == resumed['candidate'] == str(target)
    assert pointer.resolve() == target and sha(src) == before
    assert not target.with_suffix('.checkpoint.json').exists()
    with sqlite3.connect(target) as conn:
        vectors = conn.execute('SELECT chunk_id, vector FROM embeddings ORDER BY chunk_id').fetchall()
        current = {r[0] for r in conn.execute(
            'SELECT c.id FROM chunks c JOIN source_versions v ON v.id = c.source_version_id'
            ' WHERE v.is_current = 1')}
    # Committed vectors of current chunks survive the resume; retired ones are pruned.
    assert all(row in vectors for row in committed if row[0] in current)
    print('synthetic cold/resumed seconds:', json.dumps({'cold': cold['seconds'], 'resumed': resumed['seconds']}))


@pytest.mark.parametrize('phase', ['eval', 'quality', 'self_check'])
def test_deadline_outside_build_still_receipts_and_safe_pointer(tmp_path, phase):
    src, vault, pointer = setup(tmp_path)
    def slow(*a, **kw):
        time.sleep(2)
    settings = dict(evaluate=lambda *a: (scores(), scores(), {}),
                    self_check=lambda *a: {}, quality_evaluate=lambda *a, **kw: ({}, {}))
    settings[{'eval': 'evaluate', 'quality': 'quality_evaluate', 'self_check': 'self_check'}[phase]] = slow
    result = nightly.run(pointer, vault, tmp_path / 'stage', tmp_path / 'receipts',
        tmp_path / 'gold', tmp_path / 'runner', embedder=QueryFake(), deadline_seconds=.5, **settings)
    assert Path(result['receipt']).is_file()
    assert result['seconds']['total'] < 1.5
    if phase == 'quality':
        assert result['outcome'] == 'SWITCH' and result['serving_quality']['status'] == 'deferred'
    else:
        assert result['outcome'] == 'INCOMPLETE' and pointer.resolve() == src
        if phase == 'self_check':
            assert result['rollback'] == 'restored_previous'
        candidate = Path(result['candidate'])
        frozen_before = sha(candidate)
        resumed = nightly.run(pointer, vault, tmp_path / 'stage', tmp_path / 'receipts',
            tmp_path / 'gold', tmp_path / 'runner', embedder=QueryFake(),
            evaluate=lambda *a: (scores(), scores(), {}), self_check=lambda *a: {})
        assert resumed['outcome'] == 'SWITCH'
        assert resumed['reused_frozen_from'] == str(candidate)
        assert pointer.resolve() == candidate and sha(candidate) == frozen_before


def test_frozen_reuse_ignores_symlinks_bad_contract_and_stale_source(tmp_path):
    src, vault, pointer = setup(tmp_path)
    staging = tmp_path / 'stage'
    staging.mkdir()
    target = staging / 'nightly-frozen.db'
    successor(src, target, vault, embedder=QueryFake())
    (staging / 'nightly-link.db').symlink_to(target)
    (staging / 'nightly-dangling.db').symlink_to(staging / 'absent.db')
    assert nightly.reusable_frozen_target(staging, sha(src), vault) == target
    assert nightly.reusable_frozen_target(staging, 'a' * 64, vault) is None
    (vault / 'a.md').write_text('# Source changed again\nNot fresh.')
    assert nightly.reusable_frozen_target(staging, sha(src), vault) is None


def test_successful_receipt_drift_report_is_ids_only(tmp_path):
    from test_nightly import run
    report = {'renamed': 1, 'stale': 0, 'ids': ['q1'], 'paths': ['SECRET/private.md']}
    result, src, pointer = run(tmp_path,
        evaluate=lambda *a: (scores(), scores(), {'gold_drift': report}))
    saved = json.loads(Path(result['receipt']).read_text())
    assert saved['outcome'] == 'SWITCH'
    assert saved['gold_drift'] == saved['eval']['gold_drift'] == {'renamed': 1, 'stale': 0, 'ids': ['q1']}
    assert 'SECRET' not in json.dumps(saved)
