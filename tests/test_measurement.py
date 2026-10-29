"""Measurement consumes aggregate-only fixtures; no live writes or model downloads."""
import json
import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from quoth import nightly
from quoth.cli import main
from test_freshness import accepted
from test_nightly import run, scores


def golden_v3_fixture():
    origins = ['real_user'] * 20 + ['assistant_query'] * 12 + ['authored'] * 32
    rows = []
    for index, origin in enumerate(origins):
        abstain = index >= 54
        rows.append({
            'id': f'v3-{index + 1:02d}', 'question': f'SYNTHETIC PRIVATE QUERY {index + 1}',
            'lang': 'en', 'kind': 'unanswerable' if abstain else 'fact',
            'domain': 'fixture', 'expected_paths': [] if abstain else ['a.md'],
            'answer_hint': None if abstain else 'fixture', 'abstain': abstain,
            'post_snapshot': False, 'asked_at': '2026-10-01',
            'source_kind': 'authored' if origin == 'authored' else 'telegram',
            'origin': origin, 'temporal_kind': 'none', 'expected_event': None,
        })
    return rows


def write_jsonl(path, rows):
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    return hashlib.sha256(path.read_bytes()).hexdigest()


def quality(*args, **kwargs):
    return {**scores(), 'evidence_latency_ms': {'n': 2, 'p50': 2.0, 'p95': 5.0}}, {}


def test_skip_measures_once_daily_and_readback(tmp_path):
    src, vault = accepted(tmp_path)
    pointer = tmp_path / 'serving.db'
    pointer.symlink_to(src)
    calls = []
    def measure(db, *args, **kwargs):
        calls.append(db)
        return quality()
    args = (pointer, vault, tmp_path / 'stage', tmp_path / 'receipts',
            tmp_path / 'golden', tmp_path / 'private')
    first = nightly.run(*args, quality_evaluate=measure)
    second = nightly.run(*args, quality_evaluate=measure)
    assert first['outcome'] == second['outcome'] == 'SKIP'
    assert calls == [src]
    assert first['serving_quality']['status'] == 'measured'
    assert second['serving_quality']['status'] == 'cached'
    assert second['serving_quality']['measured_at'] == first['serving_quality']['measured_at']
    assert json.loads(open(second['receipt']).read())['serving_quality'] == second['serving_quality']
    assert first['serving_quality']['metrics']['private']['hit@10'] == 1
    assert first['serving_quality']['metrics']['synthetic']['recall@10'] == 1
    assert first['serving_quality']['item_ids'] == {'private': ['q1'], 'synthetic': ['q1']}
    assert str(src) not in json.dumps(first['serving_quality'])


@pytest.mark.parametrize('outcome', ['SWITCH', 'HOLD'])
def test_quality_measures_actual_served_snapshot(tmp_path, outcome):
    measured = []
    def measure(db, *args, **kwargs):
        measured.append(db)
        return quality()
    pair = lambda *args: (scores(), scores(outcome == 'SWITCH'), {})
    r, src, pointer = run(tmp_path, evaluate=pair, quality_evaluate=measure)
    assert r['outcome'] == outcome
    assert measured == [pointer.resolve()]
    assert r['serving_quality']['snapshot_id'] == (r['new'] if outcome == 'SWITCH' else r['old'])['snapshot_id']


def test_quality_failure_is_sanitized_and_not_retried_today(tmp_path):
    def fail(*args, **kwargs):
        raise RuntimeError('/private/SECRET query text')
    r, src, pointer = run(tmp_path, quality_evaluate=fail)
    assert r['outcome'] == 'SWITCH'
    assert r['serving_quality']['status'] == 'unavailable'
    assert 'SECRET' not in json.dumps(r)
    second = nightly.run(pointer, tmp_path / 'vault', tmp_path / 'staging', tmp_path / 'receipts',
                         tmp_path / 'golden', tmp_path / 'runner', quality_evaluate=fail)
    assert second['outcome'] == 'SKIP'
    assert second['serving_quality']['status'] == 'unavailable'
    assert second['serving_quality']['cached'] is True


def test_real_quality_skip_cli_with_synthetic_retriever(tmp_path, monkeypatch, capsys):
    import numpy as np
    from conftest import gate_set
    from quoth.evals import convert
    from test_refresh import FakeEmbedder
    from quoth import quality as module
    from quoth.refresh import sha
    src, vault = accepted(tmp_path)
    pointer = tmp_path / 'serving.db'
    pointer.symlink_to(src)
    row = {'id': 'fixture-1', 'question': 'PRIVATE QUERY', 'kind': 'ordinary',
           'expected_paths': ['a.md']}
    golden = tmp_path / 'golden.jsonl'
    converted = gate_set(golden, [row])
    (tmp_path / 'real-questions-v2.jsonl').write_text(json.dumps(row) + '\n')
    v3 = tmp_path / 'golden-v3.jsonl'
    v3_sha256 = write_jsonl(v3, golden_v3_fixture())
    v3_set = tmp_path / 'golden-v3.evalset.jsonl'
    convert('golden-v3', v3, v3_set)
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
    monkeypatch.setattr(module, 'get_embedder', lambda *args, **kw: QueryFake())
    before = sha(src), pointer.readlink()
    receipts = tmp_path / 'receipts'
    args = ['refresh-nightly', '--pointer', str(pointer), '--vault', str(vault),
        '--staging', str(tmp_path / 'stage'), '--receipts', str(receipts),
        '--private-runner', str(runner), '--gate-set', str(converted), '--quality-set', str(v3_set)]
    assert main(args) == 0
    out, err = capsys.readouterr()
    assert not out and not err  # SKIP stays silent for cron
    saved = json.loads(next(receipts.glob('*.json')).read_text())
    q = saved['serving_quality']
    assert saved['outcome'] == 'SKIP' and q['status'] == 'measured'
    for dataset in ('private', 'synthetic'):
        assert q['metrics'][dataset]['hit@5'] == 1
        assert q['metrics'][dataset]['hit@10'] == 1
        assert q['metrics'][dataset]['mrr@10'] == 1
        assert q['metrics'][dataset]['recall@10'] == 1
    v3_metrics = q['metrics']['golden_v3']
    assert v3_metrics['questions'] == 64
    assert v3_metrics['n'] == v3_metrics['judged_non_abstain'] == 54
    assert v3_metrics['abstain'] == v3_metrics['abstain_with_nonempty_bundle@10'] == 10
    assert v3_metrics['dataset_sha256'] == v3_sha256
    assert v3_metrics['measurement_kind'] == 'source_path_retrieval'
    assert v3_metrics['answer_level_accuracy'] == 'not_measured'
    assert {origin: values['questions'] for origin, values in v3_metrics['per_origin'].items()} == {
        'real_user': 20, 'assistant_query': 12, 'authored': 32}
    for values in v3_metrics['per_origin'].values():
        assert values['hit@5'] == values['hit@10'] == values['mrr@10'] == 1
    assert q['evidence_latency_ms']['n'] == 66
    assert 0 <= q['evidence_latency_ms']['p50'] <= q['evidence_latency_ms']['p95']
    assert q['seconds']['golden_v3'] >= 0
    raw = json.dumps(q)
    assert 'PRIVATE QUERY' not in raw and 'a.md' not in raw and str(tmp_path) not in raw
    assert main(args) == 0
    capsys.readouterr()
    runs = sorted((json.loads(p.read_text()) for p in receipts.glob('*.json')), key=lambda r: r['recorded_at'])
    assert len(runs) == 2 and runs[-1]['outcome'] == 'SKIP'
    assert runs[-1]['serving_quality']['status'] == 'cached'
    assert (sha(src), pointer.readlink()) == before
    print('synthetic quality CLI:', json.dumps(q, sort_keys=True))


def test_golden_v3_schema_package_hash_and_absent_set_are_fail_closed(tmp_path):
    from quoth.evals.convert import ConvertError, convert_in_memory
    from quoth.quality import validate
    path = tmp_path / 'golden-v3.jsonl'
    expected = write_jsonl(path, golden_v3_fixture())
    evalset = convert_in_memory('golden-v3', path)
    assert len(evalset.rows) == 64 and evalset.manifest['source']['sha256'] == expected
    (tmp_path / 'manifest.json').write_text(json.dumps(
        {'schema': 'quoth-private-golden-package/v1', 'dataset': {'sha256': '0' * 64}}))
    with pytest.raises(ConvertError, match='manifest sha256'):
        convert_in_memory('golden-v3', path)
    (tmp_path / 'manifest.json').unlink()
    broken = golden_v3_fixture()
    del broken[0]['origin']
    write_jsonl(path, broken)
    with pytest.raises(ConvertError, match='not a golden-v3 row'):
        convert_in_memory('golden-v3', path)
    receipt = {'schema': 'quoth-serving-quality/v1', 'status': 'measured',
        'snapshot_id': 'a' * 64, 'snapshot_release': 'fixture.db',
        'measured_at': '2026-10-09T21:00:00Z', 'day': '2026-10-09',
        'metrics': scores(), 'item_ids': {'private': ['q1'], 'synthetic': ['q1']},
        'evidence_latency_ms': {'n': 2, 'p50': 1, 'p95': 2}}
    checked = validate(receipt)
    assert checked['golden_v3'] == 'unavailable_not_configured'
    assert 'golden_v3' not in checked['metrics']


def test_daily_new_day_new_snapshot_and_invalid_cache(tmp_path):
    from quoth.quality import daily
    receipts = tmp_path / 'receipts'
    receipts.mkdir()
    (receipts / 'invalid.json').write_text('{"serving_quality":{"query":"PRIVATE"}}')
    identity = 'a' * 64
    day1 = datetime(2026, 10, 9, 23, 59, tzinfo=UTC)
    args = (tmp_path / 'fixture.db', identity, receipts, tmp_path / 'gold', tmp_path / 'runner')
    calls = []
    def measure(*a, **kw):
        calls.append(a[0])
        return quality()
    first = daily(*args, evaluator=measure, now=day1)
    (receipts / 'first.json').write_text(json.dumps({'schema': 'quoth-nightly/v1', 'serving_quality': first}))
    assert daily(*args, evaluator=measure, now=day1)['cached'] is True
    assert daily(*args, evaluator=measure, now=datetime(2026, 10, 10, tzinfo=UTC))['status'] == 'measured'
    assert daily(args[0], 'b' * 64, *args[2:], evaluator=measure, now=day1)['status'] == 'measured'
    assert len(calls) == 3


def test_quality_bad_values_never_persist(tmp_path):
    from quoth.quality import daily
    def invalid(*a, **kw):
        result, evidence = quality()
        result['private']['hit@10'] = float('nan')
        result['private']['query'] = 'PRIVATE'
        return result, evidence
    q = daily(tmp_path / 'fixture.db', 'a' * 64, tmp_path, tmp_path / 'gold',
              tmp_path / 'runner', evaluator=invalid)
    assert q['status'] == 'unavailable'
    assert 'PRIVATE' not in json.dumps(q) and 'NaN' not in json.dumps(q)


def test_quality_after_probe_rollback_follows_old_pointer(tmp_path):
    measured = []
    def measure(db, *a, **kw):
        measured.append(db)
        return quality()
    def fail(*a):
        raise ValueError('PRIVATE')
    r, src, pointer = run(tmp_path, self_check=fail, quality_evaluate=measure)
    assert r['outcome'] == 'FAIL'
    assert measured == [src]
    assert r['serving_quality']['snapshot_id'] == r['old']['snapshot_id']
