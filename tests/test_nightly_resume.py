"""Nightly checkpoint reuse on synthetic vaults, without live serving/model access."""
import json
import os
from pathlib import Path
import sqlite3

import pytest

from quoth import nightly
from quoth.freshness import successor
from quoth.refresh import sha
from test_nightly import scores, setup
from test_refresh import FakeEmbedder
from conftest import gate_set


def interrupted(src, vault, target):
    """Commit one batch, interrupt the second, and retain the durable target."""
    (vault / 'new.md').write_text('# New\nSynthetic evidence.')
    calls = 0

    def crash():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt('synthetic interruption')

    target.parent.mkdir(parents=True, exist_ok=True)
    with pytest.raises(KeyboardInterrupt):
        successor(src, target, vault, embedder=FakeEmbedder(crash), batch_size=1)
    assert calls == 2
    assert target.is_file() and target.with_suffix('.checkpoint.json').is_file()
    assert not target.with_suffix('.manifest.json').exists()
    with sqlite3.connect(target) as conn:
        return conn.execute('SELECT chunk_id, vector FROM embeddings ORDER BY chunk_id').fetchall()


def invoke(tmp_path, pointer, vault, **settings):
    args = dict(embedder=FakeEmbedder(),
        evaluate=lambda *a: (scores(), scores(), {'synthetic_fixture': True}),
        self_check=lambda *a: {'synthetic_fixture': True})
    args.update(settings)
    return nightly.run(pointer, vault, tmp_path / 'staging', tmp_path / 'receipts',
        tmp_path / 'golden', tmp_path / 'runner', **args)


def test_interrupted_nightly_reuses_exact_target_and_committed_batch(tmp_path):
    src, vault, pointer = setup(tmp_path)
    target = tmp_path / 'staging' / 'nightly-20261009T214906Z-fixture.db'
    committed = interrupted(src, vault, target)
    before = sha(src)
    calls = []

    def build(old, candidate, root, **settings):
        calls.append((candidate, settings.get('resume')))
        return successor(old, candidate, root, **settings)

    receipt = invoke(tmp_path, pointer, vault, build=build)
    assert receipt['outcome'] == 'SWITCH'
    assert calls == [(target, True)]
    assert receipt['candidate'] == receipt['resumed_from'] == str(target)
    assert pointer.resolve() == target
    assert sha(src) == before
    assert not target.with_suffix('.checkpoint.json').exists()
    manifest = json.loads(target.with_suffix('.manifest.json').read_text())
    assert manifest['embed']['embedded'] == 1
    with sqlite3.connect(target) as conn:
        rows = conn.execute('SELECT chunk_id, vector FROM embeddings ORDER BY chunk_id').fetchall()
        current = {r[0] for r in conn.execute(
            'SELECT c.id FROM chunks c JOIN source_versions v ON v.id = c.source_version_id'
            ' WHERE v.is_current = 1')}
    # Committed vectors of current chunks survive; retired-version vectors are pruned.
    assert all(row in rows for row in committed if row[0] in current)
    assert {r[0] for r in rows} == current
    saved = json.loads(Path(receipt['receipt']).read_text())
    assert saved['resumed_from'] == str(target)
    # Closed receipt surface: only one new field; no copied checkpoint or note data.
    assert set(saved) == {'schema', 'gate', 'outcome', 'reasons', 'seconds', 'receipt_id',
        'old', 'candidate', 'resumed_from', 'current_metrics', 'candidate_metrics', 'eval',
        'lost_gold_ids', 'gate_mode', 'excused_lost_gold', 'new', 'self_check', 'removed',
        'serving_quality', 'recorded_at'}
    assert 'Synthetic evidence' not in json.dumps(saved)


@pytest.mark.parametrize('field', ['code_digests', 'index_code_digests', 'source_sha256',
                                   'model_manifest_sha256', 'target', 'extra'])
def test_incompatible_checkpoint_ignored_for_fresh_token(tmp_path, field):
    src, vault, pointer = setup(tmp_path)
    target = tmp_path / 'staging' / 'nightly-incompatible.db'
    interrupted(src, vault, target)
    path = target.with_suffix('.checkpoint.json')
    data = json.loads(path.read_text())
    if field in ('code_digests', 'index_code_digests'):
        data[field]['chunker.py'] = 'mismatched-code'
    else:
        data[field] = 'mismatched-contract'
    path.write_text(json.dumps(data))
    checkpoint_before = path.read_bytes()
    receipt = invoke(tmp_path, pointer, vault)
    assert receipt['outcome'] == 'SWITCH'
    assert receipt['candidate'] != str(target)
    assert 'resumed_from' not in receipt
    assert path.read_bytes() == checkpoint_before and target.exists()


@pytest.mark.parametrize('outcome', ['HOLD', 'FAIL'])
def test_resumed_candidate_still_gated_and_rolled_back_on_failed_check(tmp_path, outcome):
    src, vault, pointer = setup(tmp_path)
    target = tmp_path / 'staging' / 'nightly-interrupted.db'
    interrupted(src, vault, target)

    def fail(*a):
        raise RuntimeError('synthetic probe failure')

    settings = ({'evaluate': lambda *a: (scores(), scores(False, 0, 0), {})}
                if outcome == 'HOLD' else {'self_check': fail})
    receipt = invoke(tmp_path, pointer, vault, **settings)
    assert receipt['outcome'] == outcome
    assert receipt['resumed_from'] == str(target)
    assert pointer.resolve() == src
    assert target.with_suffix('.manifest.json').exists()
    assert 'removed' not in receipt
    if outcome == 'FAIL':
        assert receipt['rollback'] == 'restored_previous'
        assert receipt['reasons'] == ['self_check_failed']


def test_newest_compatible_checkpoint_wins_not_newest_invalid(tmp_path):
    src, vault, pointer = setup(tmp_path)
    staging = tmp_path / 'staging'
    targets = [staging / ('nightly-' + name + '.db') for name in ('old', 'new', 'invalid')]
    for i, target in enumerate(targets, 1):
        interrupted(src, vault, target)
        os.utime(target.with_suffix('.checkpoint.json'), ns=(i, i))
    targets[2].with_suffix('.checkpoint.json').write_text('{malformed')
    receipt = invoke(tmp_path, pointer, vault)
    assert receipt['outcome'] == 'SWITCH'
    assert receipt['resumed_from'] == str(targets[1])
    assert targets[0].with_suffix('.checkpoint.json').exists()
    assert targets[2].with_suffix('.checkpoint.json').exists()


@pytest.mark.parametrize('invalid', ['manifest', 'missing_target', 'target_symlink',
                                    'checkpoint_symlink', 'manifest_symlink'])
def test_non_resumable_targets_ignored(tmp_path, invalid):
    src, vault, pointer = setup(tmp_path)
    target = tmp_path / 'staging' / 'nightly-invalid.db'
    interrupted(src, vault, target)
    checkpoint = target.with_suffix('.checkpoint.json')
    manifest = target.with_suffix('.manifest.json')
    if invalid == 'manifest':
        manifest.write_text('{}')
    elif invalid == 'missing_target':
        target.unlink()
    elif invalid == 'target_symlink':
        target.unlink()
        target.symlink_to(src)
    elif invalid == 'checkpoint_symlink':
        real = checkpoint.with_name('foreign.json')
        checkpoint.rename(real)
        checkpoint.symlink_to(real)
    else:
        manifest.symlink_to(tmp_path / 'missing-manifest')
    receipt = invoke(tmp_path, pointer, vault)
    assert receipt['outcome'] == 'SWITCH'
    assert receipt['candidate'] != str(target)
    assert 'resumed_from' not in receipt


def test_busy_cli_is_silent_and_does_not_inspect_checkpoints(tmp_path, monkeypatch, capsys):
    import fcntl
    from quoth.cli import main

    src, vault, pointer = setup(tmp_path)
    target = tmp_path / 'staging' / 'nightly-interrupted.db'
    interrupted(src, vault, target)
    before = sha(target), target.with_suffix('.checkpoint.json').read_bytes()

    def unexpected(*a, **kw):
        raise AssertionError('BUSY must return before checkpoint discovery')

    monkeypatch.setattr(nightly, 'resumable_target', unexpected)
    receipts = tmp_path / 'receipts'
    with pointer.with_name('serving.db.refresh.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert main(['refresh-nightly', '--pointer', str(pointer), '--vault', str(vault),
            '--staging', str(target.parent), '--receipts', str(receipts),
            '--gate-set', str(tmp_path / 'golden.jsonl'),
            '--private-runner', str(tmp_path / 'runner')]) == 0
    assert not capsys.readouterr().out
    assert not list(receipts.glob('*.json'))
    assert (sha(target), target.with_suffix('.checkpoint.json').read_bytes()) == before
    assert pointer.resolve() == src


def test_cli_interrupted_resumed_switch_with_real_pair_scoring(tmp_path, monkeypatch, capsys):
    import numpy as np
    from quoth import quality
    from quoth.cli import main
    from quoth.evidence import evidence_bundle
    from quoth.search import search
    from quoth.serving import open_snapshot

    src, vault, pointer = setup(tmp_path)
    (vault / 'new.md').write_text('# New\nSynthetic evidence.')
    row = {'id': 'fixture-1', 'question': 'Synthetic evidence', 'kind': 'ordinary',
           'expected_paths': ['a.md', 'deleted.md']}
    golden = gate_set(tmp_path / 'golden.jsonl', [row])
    (tmp_path / 'real-questions-v2.jsonl').write_text(json.dumps(row) + '\n')
    runner = tmp_path / 'runner.py'
    runner.write_text('''def validate(rows):
    assert len(rows) == 1
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
    monkeypatch.setattr(quality, 'get_embedder', lambda *a, **kw: QueryFake())

    def synthetic_check(selected, identity):
        # Real read-only admission/retrieval; deterministic local model instead
        # of the production stdio/model self-check (already covered separately).
        conn, admitted = open_snapshot(selected.resolve())
        try:
            assert admitted == identity
            assert conn.execute('PRAGMA query_only').fetchone()[0] == 1
            hits = evidence_bundle(search(conn, 'Synthetic evidence', k=1,
                retriever='dense', model='bge-m3', embedder=QueryFake()), k=1)['hits']
            assert len(hits) == 1
        finally:
            conn.close()
        return {'synthetic_read_only_evidence': True, 'snapshot_id': identity}

    calls = 0

    def crash_second_batch():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt('synthetic CLI interruption')

    embedder = QueryFake(crash_second_batch)
    original = nightly.run

    def configured(*args, **kwargs):
        return original(*args, **kwargs, embedder=embedder, batch_size=1,
                        self_check=synthetic_check)

    monkeypatch.setattr(nightly, 'run', configured)
    staging, receipts = tmp_path / 'staging', tmp_path / 'receipts'
    args = ['refresh-nightly', '--pointer', str(pointer), '--vault', str(vault),
        '--staging', str(staging), '--receipts', str(receipts),
        '--gate-set', str(golden), '--private-runner', str(runner)]
    before = sha(src)
    with pytest.raises(KeyboardInterrupt):
        main(args)
    assert not capsys.readouterr().out
    assert pointer.resolve() == src
    assert not list(receipts.glob('*.json'))
    checkpoint = next(staging.glob('nightly-*.checkpoint.json'))
    target = Path(json.loads(checkpoint.read_text())['target'])
    embedder = QueryFake()
    assert main(args) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith('SWITCH quoth-nightly receipt=')
    saved = json.loads(Path(line.split('receipt=', 1)[1]).read_text())
    assert saved['outcome'] == 'SWITCH'
    assert saved['candidate'] == saved['resumed_from'] == str(target)
    assert pointer.resolve() == target and sha(src) == before
    assert not checkpoint.exists()
    assert saved['serving_quality']['status'] == 'measured'
    assert saved['current_metrics']['synthetic']['recall@10'] == 1
    assert saved['candidate_metrics']['synthetic']['recall@10'] == 1
    manifest = json.loads(target.with_suffix('.manifest.json').read_text())
    assert manifest['embed']['embedded'] == 1
    print('synthetic interrupted/resumed CLI receipt readback:', json.dumps({
        'schema': saved['schema'], 'outcome': saved['outcome'],
        'candidate': target.name, 'resumed_from': Path(saved['resumed_from']).name,
        'reasons': saved['reasons'], 'current_metrics': saved['current_metrics'],
        'candidate_metrics': saved['candidate_metrics'], 'lost_gold_ids': saved['lost_gold_ids'],
        'seconds': saved['seconds'], 'remaining_chunks_embedded': manifest['embed']['embedded'],
        'serving_quality_status': saved['serving_quality']['status'],
        'pointer_matches_candidate': pointer.resolve() == target,
        'source_unchanged': sha(src) == before, 'checkpoint_removed': not checkpoint.exists(),
        'self_check': 'synthetic read-only evidence admission/retrieval'}, sort_keys=True))



def test_snapshot_identity_ignores_atime_but_not_content_changes():
    from types import SimpleNamespace
    from quoth.serving import file_identity

    base = dict(st_dev=1, st_ino=2, st_size=10, st_atime_ns=100, st_mtime_ns=200, st_ctime_ns=300)
    st = lambda **kw: SimpleNamespace(**{**base, **kw})
    assert file_identity(st(st_atime_ns=999)) == file_identity(st())  # hashing the file bumped atime
    for change in ({'st_mtime_ns': 201}, {'st_ctime_ns': 301}, {'st_size': 11}, {'st_ino': 3}):
        assert file_identity(st(**change)) != file_identity(st()), change
