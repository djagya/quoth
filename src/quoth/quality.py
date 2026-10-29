"""Daily aggregate quality for a frozen served snapshot; never persist payloads."""
from __future__ import annotations

import json
import math
import re
import time
from datetime import UTC, datetime
from pathlib import Path

from .dense import _MATRIX_CACHE
from .embed.backends import get_embedder
from .evals.convert import read_jsonl
from .evals.format import ORIGINS
from .evals.gate import (CODE_FILES as GATE_CODE_FILES, SnapshotScorer, as_set,
                         gate_rows, gold_missing, input_path, quality_set_identity, quality_set_summary,
                         score_dataset)
from .gold_drift import ExcessiveDrift, daily_source, report_only, resolve
from .freshness import digest
from .refresh import sha

TOKEN = re.compile(r'[A-Za-z0-9._-]{1,128}')
SOURCE = re.compile(r'[A-Za-z0-9._:/-]{1,256}')
DATASETS = ('private', 'synthetic', 'golden_v3')
METRICS = ('n', 'hit@5', 'hit@10', 'mrr@10', 'recall@10')


def percentile(values, fraction):
    """Nearest-rank percentile, in milliseconds, over actual evidence calls."""
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def evaluate(db: Path, gate_set, private_runner: Path, *, quality_set=None):
    """Score the served snapshot on the private v2 set, the gate set and the quality set.

    Retrieval and per-row scores are the harness's (`quoth.evals.gate`); the private v2
    runner keeps supplying hit@10/MRR@10 for its own set. Output keeps the
    quoth-serving-quality/v1 shape (the quality set fills the ``golden_v3`` block).
    """
    # Reuse the frozen private-v2 validation/scoring contract, without its writer.
    from .nightly import private_adapter
    gate = as_set(gate_set)
    quality = as_set(quality_set) if quality_set is not None else None
    inputs = {'synthetic': gate.path, 'private': private_runner.parent / 'real-questions-v2.jsonl',
              'private_runner': private_runner}
    if quality is not None:
        inputs['golden_v3'] = quality.path
    inputs = {name: path for name, path in inputs.items() if path is not None}
    hashes = {name: sha(path) for name, path in inputs.items()}
    adapter = private_adapter(private_runner)
    code = {name: sha(Path(__file__).parent / name) for name in
            ('quality.py', 'nightly.py', *GATE_CODE_FILES)}
    before = sha(db)
    rows = {'private': read_jsonl(inputs['private']), 'synthetic': gate_rows(gate)}
    if quality is not None:
        rows['golden_v3'] = gate_rows(quality)
    original_paths = {name: {q['id']: q['expected_paths'] for q in questions}
                      for name, questions in rows.items()}
    old, source, vault = daily_source(db)
    rows, aliases, drift = resolve(rows, old, source)
    adapter.validate(rows['private'])
    if gold_missing(rows['synthetic']):
        raise ValueError('quality_gold_missing')
    embedder = get_embedder('bge-m3', threads=2)
    result, latency, timings = {}, [], {}
    try:
        with SnapshotScorer(db, embedder=embedder) as scorer:
            for name, questions in rows.items():
                start = time.monotonic()
                scored, abstain = score_dataset(
                    scorer, questions, original_paths[name], aliases, old,
                    adapter=adapter if name == 'private' else None,
                    keep_abstain=name == 'golden_v3', latency=latency)
                if name == 'golden_v3':
                    by_id = {r['id']: r for r in scored}
                    returned = {a[0]: a[2] for a in abstain}
                    observations = [
                        {'id': q['id'], 'origin': q['origin'],
                         'metrics': by_id.get(q['id']), 'returned': returned.get(q['id'], 0)}
                        for q in questions if q['id'] in by_id or q['id'] in returned]
                    dataset_sha256, repository = quality_set_identity(quality)
                    result[name] = quality_set_summary(
                        observations, dataset_sha256=dataset_sha256, repository_source=repository)
                elif not scored or len({q['id'] for q in scored}) != len(scored):
                    raise ValueError('quality_empty_or_duplicate')
                else:
                    result[name] = {'n': len(scored),
                        **{metric: sum(q[metric] for q in scored) / len(scored)
                           for metric in METRICS[1:]},
                        'items': {q['id']: bool(q['hit@10']) for q in scored}}
                timings[name] = time.monotonic() - start
    finally:
        _MATRIX_CACHE.clear()
    if sha(db) != before or hashes != {name: sha(path) for name, path in inputs.items()}:
        raise ValueError('quality_inputs_changed')
    if daily_source(db)[1] != source or code != {name: sha(Path(__file__).parent / name) for name in code}:
        raise ValueError('quality_source_changed')
    samples = [ms for ms in latency if isinstance(ms, (int, float))]
    result['evidence_latency_ms'] = {'n': len(samples),
        'p50': percentile(samples, .5), 'p95': percentile(samples, .95)}
    return result, {'input_sha256': hashes, 'source_sha256': code,
                    'gold_drift': drift, 'seconds': timings}


def reduced(result):
    """Closed allowlist prevents an evaluator or receipt leaking private content."""
    metrics, ids = {}, {}
    for name in DATASETS:
        if name not in result:
            continue
        values = result[name]
        n = values['n']
        if type(n) is not int or n <= 0:
            raise ValueError('quality_n_invalid')
        metrics[name] = {'n': n}
        for key in METRICS[1:]:
            if key in values:
                value = values[key]
                if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                    raise ValueError('quality_metric_invalid')
                metrics[name][key] = value
        if not {'hit@10', 'mrr@10', 'recall@10'} <= metrics[name].keys():
            raise ValueError('quality_metric_missing')
        if name == 'golden_v3' and 'hit@5' not in metrics[name]:
            raise ValueError('quality_golden_hit5_missing')
        item_ids = sorted(values['items'])
        expected_ids = n
        if name == 'golden_v3':
            questions = values.get('questions')
            abstain = values.get('abstain')
            nonempty = values.get('abstain_with_nonempty_bundle@10')
            if (type(questions) is not int or questions <= 0
                    or values.get('judged_non_abstain') != n
                    or type(abstain) is not int or abstain < 0 or questions != n + abstain
                    or type(nonempty) is not int or not 0 <= nonempty <= abstain
                    or not re.fullmatch(r'[a-f0-9]{64}', values.get('dataset_sha256', ''))
                    or not isinstance(values.get('repository_source'), str)
                    or not SOURCE.fullmatch(values['repository_source'])
                    or values.get('measurement_kind') != 'source_path_retrieval'
                    or values.get('answer_level_accuracy') != 'not_measured'):
                raise ValueError('quality_golden_v3_metadata_invalid')
            per_origin = values.get('per_origin')
            if (not isinstance(per_origin, dict) or not per_origin
                    or not set(per_origin) <= set(ORIGINS)):
                raise ValueError('quality_golden_v3_origins_invalid')
            totals = dict.fromkeys(('questions', 'n', 'abstain', 'abstain_with_nonempty_bundle@10'), 0)
            safe_origins = {}
            for origin, part in per_origin.items():
                if not isinstance(part, dict):
                    raise ValueError('quality_golden_v3_origins_invalid')
                origin_questions, origin_n = part.get('questions'), part.get('n')
                origin_abstain, origin_nonempty = part.get('abstain'), part.get('abstain_with_nonempty_bundle@10')
                if (type(origin_questions) is not int or origin_questions <= 0
                        or type(origin_n) is not int or origin_n <= 0
                        or part.get('judged_non_abstain') != origin_n
                        or type(origin_abstain) is not int or origin_questions != origin_n + origin_abstain
                        or type(origin_nonempty) is not int or not 0 <= origin_nonempty <= origin_abstain):
                    raise ValueError('quality_golden_v3_origins_invalid')
                safe_origins[origin] = {key: part[key] for key in totals}
                safe_origins[origin]['judged_non_abstain'] = origin_n
                for key in METRICS[1:]:
                    value = part.get(key)
                    if (not isinstance(value, (int, float)) or isinstance(value, bool)
                            or not math.isfinite(value) or not 0 <= value <= 1):
                        raise ValueError('quality_golden_v3_origins_invalid')
                    safe_origins[origin][key] = value
                for key in totals:
                    totals[key] += part[key]
            if totals != {'questions': questions, 'n': n, 'abstain': abstain,
                           'abstain_with_nonempty_bundle@10': nonempty}:
                raise ValueError('quality_golden_v3_origins_invalid')
            metrics[name].update(questions=questions, judged_non_abstain=n, abstain=abstain,
                **{'abstain_with_nonempty_bundle@10': nonempty},
                dataset_sha256=values['dataset_sha256'],
                repository_source=values.get('repository_source'),
                measurement_kind='source_path_retrieval', answer_level_accuracy='not_measured',
                per_origin=safe_origins)
            expected_ids = questions
        if (len(item_ids) != expected_ids
                or any(not isinstance(q, str) or not TOKEN.fullmatch(q) for q in item_ids)):
            raise ValueError('quality_ids_invalid')
        ids[name] = item_ids
    if not {'private', 'synthetic'} <= metrics.keys():
        raise ValueError('quality_dataset_missing')
    latency = result['evidence_latency_ms']
    if type(latency['n']) is not int or latency['n'] <= 0:
        raise ValueError('quality_latency_invalid')
    for key in ('p50', 'p95'):
        if type(latency[key]) not in (int, float) or not math.isfinite(latency[key]) or latency[key] < 0:
            raise ValueError('quality_latency_invalid')
    if latency['p50'] > latency['p95']:
        raise ValueError('quality_latency_invalid')
    return metrics, ids, {key: latency[key] for key in ('n', 'p50', 'p95')}


def validate(receipt):
    """Read receipts as data, returning only fields produced by this module."""
    if receipt['schema'] != 'quoth-serving-quality/v1':
        raise ValueError('quality_schema_invalid')
    if receipt['status'] not in ('measured', 'cached', 'unavailable'):
        raise ValueError('quality_status_invalid')
    if not re.fullmatch(r'[a-f0-9]{64}', receipt['snapshot_id']):
        raise ValueError('quality_identity_invalid')
    release = receipt['snapshot_release']
    if not TOKEN.fullmatch(release):
        raise ValueError('quality_release_invalid')
    measured = datetime.fromisoformat(receipt['measured_at'].replace('Z', '+00:00'))
    offset = measured.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise ValueError('quality_date_invalid')
    if receipt['day'] != measured.date().isoformat():
        raise ValueError('quality_day_invalid')
    out = {key: receipt[key] for key in ('schema', 'status', 'snapshot_id', 'snapshot_release',
                                        'measured_at', 'day')}
    out['cached'] = receipt.get('cached') is True
    if 'context_sha256' in receipt:
        if not re.fullmatch(r'[a-f0-9]{64}', receipt['context_sha256']):
            raise ValueError('quality_context_invalid')
        out['context_sha256'] = receipt['context_sha256']
    if 'gold_drift' in receipt:
        out['gold_drift'] = report_only(receipt['gold_drift'])
    if out['status'] != 'unavailable':
        result = {name: {**values, 'items': dict.fromkeys(receipt['item_ids'][name], False)}
                  for name, values in receipt['metrics'].items() if name in DATASETS}
        result['evidence_latency_ms'] = receipt['evidence_latency_ms']
        out['metrics'], out['item_ids'], out['evidence_latency_ms'] = reduced(result)
        out['golden_v3'] = 'measured' if 'golden_v3' in out['metrics'] else 'unavailable_not_configured'
        out['seconds'] = {}
        for name in (*DATASETS, 'total'):
            value = receipt.get('seconds', {}).get(name)
            if type(value) in (int, float) and math.isfinite(value) and value >= 0:
                out['seconds'][name] = value
    else:
        out['reason'] = ('gold_drift_excessive' if receipt.get('reason') == 'gold_drift_excessive'
                         else 'quality_evaluation_unavailable')
    return out


def daily(db, identity, receipts, gate_set, private_runner, *, evaluator=evaluate,
          quality_set=None, now=None):
    """Called under refresh's pointer lock; one attempt per snapshot per UTC day.

    Failed attempts are also cached until tomorrow, not silently retried each run.
    Cache identity is the immutable DB digest, never the pointer pathname.
    """
    if not TOKEN.fullmatch(db.name):
        raise ValueError('quality_release_invalid')
    stamp = (now or datetime.now(UTC)).astimezone(UTC)
    day = stamp.date().isoformat()
    # Reject pre-repair receipts and invalidate on code, input or live-source drift.
    context: dict = {'code': {name: sha(Path(__file__).parent / name)
                for name in ('quality.py', 'nightly.py', *GATE_CODE_FILES)}}
    if evaluator is evaluate:
        try:
            context['source'] = daily_source(db)[1]
            paths = [input_path(gate_set), private_runner,
                     private_runner.parent / 'real-questions-v2.jsonl']
            if quality_set is not None:
                paths.append(input_path(quality_set))
            context['inputs'] = [sha(path) for path in paths]
        except Exception:
            context['inputs'] = 'unavailable'
    context_id = digest(context)
    for path in sorted(receipts.glob('*.json'), reverse=True):
        try:
            if path.is_symlink() or path.stat().st_size > 4 * 1024 * 1024:
                continue
            receipt = json.loads(path.read_text())
            if receipt.get('schema') != 'quoth-nightly/v1':
                continue
            cached = validate(receipt['serving_quality'])
            if (cached['day'] == day and cached['snapshot_id'] == identity
                    and cached.get('context_sha256') == context_id):
                cached['cached'] = True
                if cached['status'] == 'measured':
                    cached['status'] = 'cached'
                return cached
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    out = {'schema': 'quoth-serving-quality/v1', 'snapshot_id': identity,
           'snapshot_release': db.name, 'day': day,
           'measured_at': stamp.isoformat().replace('+00:00', 'Z'),
           'status': 'unavailable', 'cached': False, 'context_sha256': context_id}
    start = time.monotonic()
    try:
        result, evidence = evaluator(db, gate_set, private_runner, quality_set=quality_set)
        out['metrics'], out['item_ids'], out['evidence_latency_ms'] = reduced(result)
        if 'gold_drift' in evidence:
            out['gold_drift'] = report_only(evidence['gold_drift'])
        out.update(status='measured', seconds={**evidence.get('seconds', {}),
                                               'total': time.monotonic() - start})
        return validate(out)
    except ExcessiveDrift as error:
        out.update(reason='gold_drift_excessive', gold_drift=report_only(error.report))
        return validate(out)
    except Exception:
        # Not a refresh-gate failure; never persist exception text or arguments.
        return {key: out[key] for key in ('schema', 'snapshot_id', 'snapshot_release', 'day',
                                          'measured_at', 'cached', 'context_sha256')} | {
            'status': 'unavailable', 'reason': 'quality_evaluation_unavailable'}
