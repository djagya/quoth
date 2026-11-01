"""Offline nightly builder, frozen quality gate, atomic serving-pointer switch.

Receipts contain aggregates and regression IDs only, never questions/note text.
No gateway/config/scheduler mutation. Mutable state belongs outside the repository.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import redirect_stdout
from datetime import UTC, datetime
import fcntl

import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import tempfile
import time
import types
import uuid

from .dense import _MATRIX_CACHE
from .embed.backends import get_embedder
from .evals.convert import read_jsonl
from .evals.format import EvalSetError, manifest_path, vault_paths
from .evals.gate import (CODE_FILES as GATE_CODE_FILES, SnapshotScorer, as_set,
                         gate_rows, gold_missing, score_dataset)
from .evals.run import Receipt
from .evals.suite import (Suite, SuiteError, compare_suites, load_entry, load_suite,
                          run_suite_on_snapshot, without_roles)
from .freshness import model_contract, status, successor
from .gold_drift import ExcessiveDrift, report_only, resolve, snapshot_identities, snapshot_inventory
from .heartbeat import check, write_heartbeat
from .quality import daily as daily_quality, evaluate as evaluate_quality
from .refresh import (REBASE_CONTRACT_FILES, SERVING_CONTRACT_FILES, _json_write, code_digests,
                      index_code_digests, sha)
from .runtime_budget import DeadlineReached, budget
from .serving import open_snapshot, replace_pointer

# Strict "code" gate: used whenever index code changed between the serving snapshot
# and the successor (any index_code_digests difference, missing digests, or an
# --allow-index-contract-rebase run).
GATE = {'version': 'quoth-nightly/v1', 'private_hit10_drop': 0,
        'private_mrr_drop': 0, 'synthetic_recall10_drop': 0.01,
        'lost_all_gold_allowed': 0}
# Content-only gate: identical index-code digests, so the
# successor differs only in vault content. Metric drops up to CONTENT_TOLERANCE are
# accepted, and an item that loses all gold is excused only when a note its gold
# points at was changed or deleted by this refresh.
CONTENT_TOLERANCE = 0.03
CONTENT_GATE = {**GATE, 'private_hit10_drop': CONTENT_TOLERANCE,
                'private_mrr_drop': CONTENT_TOLERANCE,
                'synthetic_recall10_drop': CONTENT_TOLERANCE,
                'lost_all_gold_excused': 'gold_note_changed_or_removed'}
GATES = {'code': GATE, 'content': CONTENT_GATE}
GATE_METRICS = (('private', 'hit@10', 'private_hit10_drop'),
                ('private', 'mrr@10', 'private_mrr_drop'),
                ('synthetic', 'recall@10', 'synthetic_recall10_drop'))
GOLD_NOTE_CHANGED, GOLD_NOTE_REMOVED = 'gold_note_changed', 'gold_note_removed'
# --suite gate (docs/EVAL.md#the-nightly-suite-gate). Both snapshots are scored on the
# suite's gated sets and compared with `eval suite compare`'s rules: smoke and regression
# sets block on their tolerances and lost gold, quality sets only on a significantly worse
# paired test, tune sets are not scored. Latency rules are not gated (machine load varies
# between the two runs). Content mode floors every `max_drop` at CONTENT_TOLERANCE and
# excuses lost gold whose note this refresh changed or deleted, as the legacy gate does.
SUITE_GATE = {'version': 'quoth-nightly-suite/v1', 'latency': 'not_gated',
              'roles': {'smoke': 'blocks', 'regression': 'blocks',
                        'quality': 'blocks_when_significantly_worse', 'tune': 'not_scored'}}
SUITE_FIXTURE = 'private'  # what `eval convert` writes for sets of the owner's own vault
SUITE_SKIPPED_ROLES = ('tune',)
SUITE_CODE_FILES = ('evals/suite.py', 'evals/compare.py', 'evals/stats.py')
SUITE_RUNS = 'suite-runs'
KEEP_SUITE_RUNS = 14
RUN_TOKEN = re.compile(r'\d{8}T\d{6}Z-[0-9a-f]{12}')
DEPRECATED_RUNNER = ('quoth refresh-nightly: --private-runner is deprecated: it executes a'
                     ' Python file. Convert its set with `quoth eval convert --from private-v2`'
                     ' and gate on a suite with --suite (docs/EVAL.md#the-nightly-suite-gate).')


def gate(current, candidate, *, mode='code', note_changes=None):
    """Return (reasons, lost, excused).

    ``note_changes`` maps old-snapshot note path -> GOLD_NOTE_CHANGED/REMOVED for
    this refresh; ``current[dataset]['gold']`` maps item id -> its gold paths. Both
    are used only in content mode, and are never persisted in receipts.
    """
    thresholds = GATES[mode]
    note_changes = note_changes or {}
    reasons, lost, excused = [], {}, {}
    for dataset in ('private', 'synthetic'):
        a, b = current[dataset], candidate[dataset]
        if not a['items'] or set(a['items']) != set(b['items']):
            raise ValueError('eval_item_set_mismatch')
        for metric in ('hit@10', 'mrr@10', 'recall@10'):
            if not all(math.isfinite(x[metric]) and 0 <= x[metric] <= 1 for x in (a, b)):
                raise ValueError('eval_metric_invalid')
        lost[dataset] = sorted(q for q in a['items'] if a['items'][q] and not b['items'][q])
        excused[dataset] = []
        if mode == 'content':
            gold = a.get('gold') or {}
            for q in lost[dataset]:
                why = {note_changes[p] for p in gold.get(q, ()) if p in note_changes}
                if why:
                    excused[dataset].append({'id': q, 'reason': GOLD_NOTE_REMOVED
                                             if GOLD_NOTE_REMOVED in why else GOLD_NOTE_CHANGED})
        if set(lost[dataset]) - {e['id'] for e in excused[dataset]}:
            reasons.append(dataset + '_lost_all_gold')
    for dataset, metric, key in GATE_METRICS:
        if candidate[dataset][metric] < current[dataset][metric] - thresholds[key]:
            reasons.append(dataset + '_' + metric + '_regressed')
    return reasons, lost, excused


def gate_mode(old: Path, candidate: Path, *, allow_index_contract_rebase=False):
    """'content' when the successor differs from the serving snapshot only in vault content.

    Index-code digests must match. Under --allow-index-contract-rebase the operator has
    declared REBASE_CONTRACT_FILES changes semantics-preserving, so differences limited
    to those files still count as content-only; any other difference, or a manifest
    without digests, selects the strict 'code' gate.
    """
    manifests = [json.loads(db.with_suffix('.manifest.json').read_text()) for db in (old, candidate)]
    # Older snapshots carry only code_digests. Serving-only files (search.py) run the same
    # code against both snapshots during eval, so they never distinguish the two builds.
    key = next((k for k in ('index_code_digests', 'code_digests')
                if all(isinstance(m.get(k), dict) and m.get(k) for m in manifests)), None)
    if key is None:
        return 'code'
    digests = [{k: v for k, v in m[key].items() if k not in SERVING_CONTRACT_FILES} for m in manifests]
    differing = {k for k in set(digests[0]) | set(digests[1]) if digests[0].get(k) != digests[1].get(k)}
    allowed = REBASE_CONTRACT_FILES if allow_index_contract_rebase else frozenset()
    return 'content' if differing <= allowed else 'code'


def note_changes(old: Path, candidate: Path):
    """Old current notes changed or deleted by this refresh (path -> reason).

    A path whose identical content now lives at exactly one other path is a pure
    rename, not a content change: gold drift aliases it and it is not excused.
    """
    before, after = snapshot_inventory(old), snapshot_inventory(candidate)
    moved = {}
    for identity in after.values():
        moved[identity] = moved.get(identity, 0) + 1
    changes = {}
    for path, identity in before.items():
        if path in after:
            if after[path] != identity:
                changes[path] = GOLD_NOTE_CHANGED
        elif moved.get(identity) != 1:
            changes[path] = GOLD_NOTE_REMOVED
    return changes


def private_adapter(path: Path):
    """Import existing private v2 validator/scorer, never edit/copy its inputs.

    Use its pure validation/metrics functions rather than run(): run hardcodes
    output names and persists per-row provenance forbidden in nightly receipts.
    """
    # Do not let importlib create __pycache__ beside read-only private inputs.
    module = types.ModuleType('quoth_private_gate')
    module.__file__ = str(path)
    exec(compile(path.read_bytes(), str(path), 'exec'), module.__dict__)
    return module


def gate_set_evidence(gate_set):
    """Content-free identity of the gate set for the receipt (additive `eval.gate_set`)."""
    source = gate_set.manifest.get('source') or {}
    out = {'schema': gate_set.manifest.get('schema'), 'sha256': gate_set.sha256,
           'rows': len(gate_set.rows)}
    if source.get('format'):
        out['source_format'] = source['format']
    return out


def evaluate_pair(current: Path, candidate: Path, gate_set, private_runner: Path):
    """Score both snapshots on the gate set and the private v2 set (harness retrieval)."""
    try:
        return _evaluate_pair(current, candidate, gate_set, private_runner)
    finally:
        _MATRIX_CACHE.clear()


def _evaluate_pair(current: Path, candidate: Path, gate_set, private_runner: Path):
    gate_set = as_set(gate_set)
    private_golden = private_runner.parent / 'real-questions-v2.jsonl'
    inputs = [p for p in (gate_set.path, private_golden, private_runner) if p is not None]
    datasets = {str(p): sha(p) for p in inputs}
    code = {name: sha(Path(__file__).parent / name) for name in ('nightly.py', *GATE_CODE_FILES)}
    snapshot_hashes = {db: sha(db) for db in (current, candidate)}
    adapter = private_adapter(private_runner)
    rows = {'private': read_jsonl(private_golden), 'synthetic': gate_rows(gate_set)}
    original_paths = {name: {q['id']: q['expected_paths'] for q in questions}
                      for name, questions in rows.items()}
    old_inventory = snapshot_identities(current)
    rows, aliases, drift = resolve(rows, old_inventory, snapshot_inventory(candidate))
    adapter.validate(rows['private'])
    if gold_missing(rows['synthetic']):
        raise ValueError('synthetic_gold_missing')
    embedder = get_embedder('bge-m3', threads=2)
    outputs = []
    for db in (current, candidate):
        before = snapshot_hashes[db]
        result = {}
        with SnapshotScorer(db, embedder=embedder) as scorer:
            for dataset, questions in rows.items():
                scored, _ = score_dataset(scorer, questions, original_paths[dataset], aliases,
                                          old_inventory if db == current else None,
                                          adapter=adapter if dataset == 'private' else None)
                if not scored or len({r['id'] for r in scored}) != len(scored):
                    raise ValueError('eval_empty_or_duplicate')
                result[dataset] = {'n': len(scored),
                    **{m: sum(r[m] for r in scored) / len(scored)
                       for m in ('hit@10', 'mrr@10', 'recall@10')},
                    'items': {r['id']: bool(r['hit@10']) for r in scored},
                    # Gate-only: original gold paths per item; stripped from receipts.
                    'gold': {r['id']: r['gold'] for r in scored}}
        _MATRIX_CACHE.clear()
        if sha(db) != before:
            raise ValueError('eval_snapshot_changed')
        outputs.append(result)
    if datasets != {str(p): sha(p) for p in inputs}:
        raise ValueError('eval_inputs_changed')
    if code != {name: sha(Path(__file__).parent / name) for name in code}:
        raise ValueError('eval_code_changed')
    return outputs[0], outputs[1], {'input_sha256': datasets,
        'source_sha256': code,
        'gold_drift': drift,
        'gate_set': gate_set_evidence(gate_set),
        'engine': 'quoth.evals',
        'model': 'bge-m3', 'threads': 2}


class SuiteGateError(ValueError):
    """A --suite gate failure whose code is the receipt's reason (`suite_invalid`,
    `suite_incomparable`); never carries a path or the suite's content."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def gate_suite(value, fixture: str = SUITE_FIXTURE) -> tuple[Suite, list[str]]:
    """(the suite's gated part, the ids of the sets it does not score).

    The suite's fixture must be `fixture` (--suite-fixture, default `private`: the sets were
    written against this install's own vault); `load_entry` then holds every set to the
    suite's fixture or `any`. Raises SuiteGateError('suite_invalid')."""
    try:
        suite = value if isinstance(value, Suite) else load_suite(value)
        if suite.fixture != fixture:
            raise SuiteError('suite fixture is not the nightly fixture')
        gated = without_roles(suite, SUITE_SKIPPED_ROLES)
        if not gated.entries:
            raise SuiteError('suite has no gated sets')
        for entry in gated.entries:
            load_entry(gated, entry)
    except (SuiteError, EvalSetError, OSError) as error:
        raise SuiteGateError('suite_invalid') from error
    return gated, [e.id for e in suite.entries if e.role in SUITE_SKIPPED_ROLES]


def suite_identity(suite: Suite, skipped) -> dict:
    """Content-free identity of the suite for the receipt."""
    return {'name': suite.name, 'fixture': suite.fixture, 'sha256': suite.sha256,
            'roles': {e.id: e.role for e in suite.entries}, 'skipped': sorted(skipped)}


def excusable(rows, changes) -> dict:
    """{row id: reason} of rows whose (as written) vault gold this refresh changed or deleted."""
    out = {}
    for row in rows:
        why = {changes[p] for p in vault_paths(row) if p in changes}
        if why:
            out[row['id']] = GOLD_NOTE_REMOVED if GOLD_NOTE_REMOVED in why else GOLD_NOTE_CHANGED
    return out


def suite_tolerances(tolerances: dict, *, mode: str = 'code', excused=()) -> dict:
    """A set's tolerances as the nightly gate applies them: latency rules dropped; in content
    mode every `max_drop` is at least CONTENT_TOLERANCE and `excused` ids may lose gold."""
    metrics = {path: rule for path, rule in (tolerances.get('metrics') or {}).items()
               if not path.startswith('latency_ms.')}
    out = {**tolerances, 'metrics': metrics}
    if mode == 'content':
        out['metrics'] = {path: ({**rule, 'max_drop': max(rule['max_drop'], CONTENT_TOLERANCE)}
                                 if 'max_drop' in rule else rule) for path, rule in metrics.items()}
        out['excused_lost_gold'] = sorted(set(tolerances.get('excused_lost_gold') or ()) | set(excused))
    return out


def suite_drift(out_dir: Path, summaries: dict) -> dict:
    """The candidate side's gold drift over every set. Excessive drift on either side fails
    the run (ExcessiveDrift), as one excessive dataset fails the legacy gate."""
    total = {'renamed': 0, 'stale': 0, 'ids': set()}
    for label, summary in summaries.items():
        for meta in summary['sets'].values():
            data = Receipt.load(out_dir / label / meta['receipt']).data
            report = data.get('gold_drift') or {'renamed': 0, 'stale': 0, 'ids': []}
            if data.get('status') == 'gold_drift_excessive':
                raise ExcessiveDrift(report)
            if data.get('status') != 'ok':
                raise ValueError('eval_run_not_ok')
            if label == 'candidate':
                total['renamed'] += report['renamed']
                total['stale'] += report['stale']
                total['ids'] |= set(report['ids'])
    return {**total, 'ids': sorted(total['ids'])}


def evaluate_suite_pair(current: Path, candidate: Path, suite: Suite, out_dir: Path, *,
                        mode: str = 'code', note_changes=None):
    """Score both snapshots on the suite's gated sets and compare them set by set.

    Returns (`compare_suites` result, evidence). The per-set run receipts and the
    comparison stay in `out_dir` (`current/`, `candidate/`, `compare.json`)."""
    try:
        return _evaluate_suite_pair(current, candidate, suite, Path(out_dir), mode=mode,
                                    note_changes=note_changes or {})
    finally:
        _MATRIX_CACHE.clear()


def _evaluate_suite_pair(current, candidate, suite, out_dir, *, mode, note_changes):
    sets = {e.id: load_entry(suite, e) for e in suite.entries}
    tolerance_files = [suite.tolerances, *(e.tolerances for e in suite.entries)]
    inputs = dict.fromkeys([suite.path, *(p for e in suite.entries for p in (e.path, manifest_path(e.path))),
                            *(t for t in tolerance_files if t is not None)])
    datasets = {str(p): sha(p) for p in inputs}
    code = {name: sha(Path(__file__).parent / name)
            for name in ('nightly.py', *GATE_CODE_FILES, *SUITE_CODE_FILES)}
    snapshot_hashes = {db: sha(db) for db in (current, candidate)}
    excused = {sid: excusable(evalset.rows, note_changes) if mode == 'content' else {}
               for sid, evalset in sets.items()}
    embedder = get_embedder('bge-m3', threads=2)
    summaries = {}
    for label, db in (('current', current), ('candidate', candidate)):
        summaries[label] = run_suite_on_snapshot(suite, db, out_dir / label, embedder=embedder)
        _MATRIX_CACHE.clear()
        if sha(db) != snapshot_hashes[db]:
            raise ValueError('eval_snapshot_changed')
    if datasets != {str(p): sha(p) for p in inputs}:
        raise ValueError('eval_inputs_changed')
    if code != {name: sha(Path(__file__).parent / name) for name in code}:
        raise ValueError('eval_code_changed')
    drift = suite_drift(out_dir, summaries)
    comparison = compare_suites(out_dir / 'current', out_dir / 'candidate', suite,
                                tolerances_for=lambda sid, tol: suite_tolerances(
                                    tol, mode=mode, excused=excused[sid]))
    for sid, result in comparison['sets'].items():
        result['excused_reasons'] = {i: excused[sid][i]
                                     for i in result['compare'].get('excused_lost_gold', [])}
    (out_dir / 'compare.json').write_text(json.dumps(comparison, indent=2, sort_keys=True) + '\n',
                                          encoding='utf-8')
    headlines = {label: {sid: s['headline'] for sid, s in summary['sets'].items()}
                 for label, summary in summaries.items()}
    return comparison, {'input_sha256': datasets, 'source_sha256': code, 'gold_drift': drift,
                        'headlines': headlines, 'engine': 'quoth.evals.suite',
                        'model': 'bge-m3', 'threads': 2}


def suite_gate(comparison: dict) -> tuple[list, dict, dict, dict]:
    """(reasons, lost gold ids, excused {id, reason}, per-set report) of a suite comparison.

    A failing smoke or regression set holds the successor (`suite_<id>_regressed`), a
    significantly worse quality set too (`suite_<id>_quality_worse`); a comparison that is
    not possible fails the run (SuiteGateError `suite_incomparable`)."""
    if comparison.get('exit') == 3 or comparison.get('problems'):
        raise SuiteGateError('suite_incomparable')
    reasons, lost, excused, report = [], {}, {}, {}
    for sid, result in sorted(comparison['sets'].items()):
        detail = result['compare']
        if result['exit'] == 3:
            raise SuiteGateError('suite_incomparable')
        reasons_of = result.get('excused_reasons') or {}
        lost[sid] = list(detail.get('lost_gold', []))
        excused[sid] = [{'id': i, 'reason': reasons_of.get(i)} for i in detail.get('excused_lost_gold', [])]
        if result['exit'] == 1:
            reasons.append(f"suite_{sid}_{'quality_worse' if result['role'] == 'quality' else 'regressed'}")
        report[sid] = {'role': result['role'], 'result': result['result'],
                       'failed_checks': [c['metric'] for c in detail.get('checks', [])
                                         if c.get('result') == 'fail'],
                       'paired': detail.get('paired', {}),
                       'quality_failed_on': detail.get('quality_failed_on', [])}
    return reasons, lost, excused, report


def prune_suite_runs(receipts: Path, keep: int = KEEP_SUITE_RUNS) -> list[str]:
    """Keep the newest `keep` suite-run directories this module wrote; touch nothing else."""
    root = receipts / SUITE_RUNS
    if root.is_symlink() or not root.is_dir():
        return []
    runs = sorted(p for p in root.iterdir()
                  if RUN_TOKEN.fullmatch(p.name) and p.is_dir() and not p.is_symlink())
    removed = []
    for path in runs[:max(0, len(runs) - max(1, keep))]:
        shutil.rmtree(path)
        removed.append(path.name)
    return removed


PROBE_ENV = ('HERMES_HOME',)  # besides every QUOTH_* variable


def probe_env() -> dict:
    """The probe child's environment: the SDK's safe default (PATH, HOME, ...) plus this
    process's quoth variables, so `quoth mcp` finds the same state and model cache as the
    nightly run (the SDK passes nothing else through)."""
    from mcp.client.stdio import get_default_environment
    env = get_default_environment()
    env.update({k: v for k, v in os.environ.items() if k.startswith('QUOTH_') or k in PROBE_ENV})
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1])
    return env


_ERROR_CLASS = re.compile(r'\b([A-Z][A-Za-z0-9]*):')


def tool_error_class(text: str) -> str:
    """The first exception class named in a tool error's text (`EmbedderUnavailable: ...`),
    never the rest: the message can hold host paths, and receipts carry codes only."""
    m = _ERROR_CLASS.search(text)
    return m.group(1) if m else 'unknown'


def probe_payload(result) -> dict:
    """The JSON payload of the probe's tool call; a tool error or a non-JSON answer is a
    `ProbeError` with a path-free code, never a bare JSON decode error."""
    text = ''.join(c.text for c in result.content if hasattr(c, 'text'))
    if result.is_error:
        raise ProbeError('probe_tool_error', tool_error_class(text))
    try:
        return json.loads(text)
    except ValueError:
        raise ProbeError('probe_not_json', 'unknown') from None


async def stdio_probe(pointer: Path, expected_id: str, model='bge-m3'):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(command=sys.executable,
        args=['-m', 'quoth.cli', 'mcp', '--db', str(pointer)], env=probe_env())
    with tempfile.TemporaryFile(dir=pointer.parent, mode='w+') as errors:
        async with stdio_client(params, errlog=errors) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                if {t.name for t in (await session.list_tools()).tools} != {'evidence_bundle'}:
                    raise ValueError('probe_registry')
                result = await session.call_tool('evidence_bundle',
                    {'query': 'memory snapshot health check', 'k': 1, 'model': model})
                payload = probe_payload(result)
                if payload.get('provenance', {}).get('snapshot_id') != expected_id:
                    raise ValueError('probe_snapshot')
                if not payload.get('hits'):
                    raise ValueError('probe_no_evidence')
    return {'stdio': True, 'snapshot_id': expected_id, 'tools': ['evidence_bundle']}


class ProbeError(ValueError):
    """A failed self-check, with a path-free code and the tool's error class."""

    def __init__(self, code: str, error_class: str):
        super().__init__(code)
        self.code, self.error_class = code, error_class


def probe(pointer, identity):
    return asyncio.run(asyncio.wait_for(stdio_probe(pointer, identity), timeout=180))


def aggregates(result):
    return {name: {k: v for k, v in scores.items() if k not in ('items', 'gold')}
            for name, scores in result.items()}


def retention(staging, pointer, previous, held):
    """Only recognized frozen nightly outputs; never touch other builders' files."""
    keep = {pointer.resolve(strict=True), previous}
    if held:
        keep.add(Path(held).resolve())
    removed = []
    for db in staging.glob('nightly-*.db'):
        if db.is_symlink() or db.resolve() in keep:
            continue
        try:
            manifest = json.loads(db.with_suffix('.manifest.json').read_text())
            if manifest.get('db') != str(db.absolute()) or manifest.get('status') != 'frozen_current_vault_candidate':
                continue
        except (OSError, ValueError):
            continue
        # Re-read pointer before each deletion. External cutovers are not owned here.
        if db.resolve() == pointer.resolve(strict=True):
            continue
        db.unlink()
        db.with_suffix('.manifest.json').unlink()
        removed.append(db.name)
    return removed


def resumable_target(staging: Path, source_id: str) -> Path | None:
    """Newest compatible durable nightly target; called under the pointer lock.

    Match refresh's closed checkpoint contract, including model identity. Infer
    the target from the staging filename, never follow a checkpoint-supplied path.
    Incomplete backups without a durable DB and frozen outputs are not resumable.
    """
    expected = {'source_sha256': source_id, 'code_digests': code_digests(),
                'index_code_digests': index_code_digests(),
                'model_manifest_sha256': model_contract()}
    compatible = []
    for checkpoint in staging.glob('nightly-*.checkpoint.json'):
        target = checkpoint.with_suffix('').with_suffix('.db')
        manifest = target.with_suffix('.manifest.json')
        try:
            if (checkpoint.is_symlink() or not checkpoint.is_file()
                    or target.is_symlink() or not target.is_file()
                    or manifest.exists() or manifest.is_symlink()):
                continue
            if json.loads(checkpoint.read_text()) == {**expected, 'target': str(target)}:
                compatible.append((checkpoint.stat().st_mtime_ns, checkpoint.name, target))
        except (OSError, ValueError):
            continue
    return max(compatible)[2] if compatible else None


def run(*args, deadline_seconds: float = 3000, **kwargs):
    with budget(deadline_seconds):
        return _run(*args, **kwargs)


def reusable_frozen_target(staging, source_id, vault):
    """Reuse a fresh same-lineage frozen output after an eval deadline or HOLD.

    No successor base/gate change: the comparison remains against the serving DB.
    Skip foreign/symlink/incompatible artifacts; full status admission rehashes.
    """
    files = sorted(staging.glob('nightly-*.db'), key=lambda p: (p.lstat().st_mtime_ns, p.name), reverse=True)
    for target in files:
        manifest = target.with_suffix('.manifest.json')
        try:
            if target.is_symlink() or manifest.is_symlink() or not manifest.is_file():
                continue
            data = json.loads(manifest.read_text())
            if (data.get('source_db_sha256') == source_id
                    and data.get('index_code_digests') == index_code_digests()
                    and status(target, vault)['state'] == 'fresh'):
                return target
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None


def _run(pointer: Path, vault: Path, staging: Path, receipts: Path, gate_set=None,
        private_runner: Path | None = None, *, build=successor, evaluate=evaluate_pair, self_check=probe,
        quality_evaluate=evaluate_quality, quality_set=None, suite=None, suite_fixture=SUITE_FIXTURE,
        suite_evaluate=evaluate_suite_pair, **build_settings):
    """`gate_set` / `quality_set`: an eval set v1 path or an `EvalSet` (the legacy gate, with
    `private_runner`). `suite`: a suite file or `Suite` instead (the suite gate)."""
    start = time.monotonic()
    pointer, staging, receipts = pointer.absolute(), staging.absolute(), receipts.absolute()
    receipts.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(receipts, 0o700)
    token = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + uuid.uuid4().hex[:12]
    receipt_path = receipts / (token + '.json')
    receipt = {'schema': 'quoth-nightly/v1', 'gate': GATE, 'outcome': 'FAIL',
               'reasons': [], 'seconds': {}, 'receipt_id': token}
    # Same pointer always shares one lock, even if output directories differ.
    fd = os.open(pointer.with_name(pointer.name + '.refresh.lock'),
                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'outcome': 'BUSY', 'reasons': ['refresh_busy']}
        stage = 'preflight'
        switched = False
        old = None
        gated = None
        try:
            if suite is not None:
                gated, skipped = gate_suite(suite, suite_fixture)
                receipt['gate'] = SUITE_GATE
                receipt['suite'] = suite_identity(gated, skipped)
            elif gate_set is None or private_runner is None:
                raise ValueError('gate_inputs_missing')
            else:
                receipt['deprecated'] = ['private_runner']
            if not pointer.is_symlink():
                raise ValueError('symlink_required')
            old = pointer.resolve(strict=True)
            conn, old_id = open_snapshot(old)
            conn.close()
            receipt['old'] = {'db': str(old), 'snapshot_id': old_id}
            prior = status(old, vault, allow_index_contract_rebase=build_settings.get(
                'allow_index_contract_rebase', False))
            if prior['state'] == 'fresh':
                receipt['outcome'] = 'SKIP'
            else:
                if prior['state'] != 'stale':
                    raise ValueError('source_not_compatible')
                staging.mkdir(parents=True, exist_ok=True)
                if shutil.disk_usage(staging).free < 2 * old.stat().st_size:
                    raise ValueError('disk_preflight')
                candidate = resumable_target(staging, old_id)
                settings = dict(build_settings)
                if candidate is not None:
                    receipt['resumed_from'] = str(candidate)
                    settings['resume'] = True
                else:
                    candidate = reusable_frozen_target(staging, old_id, vault)
                    if candidate is not None:
                        receipt['reused_frozen_from'] = str(candidate)
                    else:
                        candidate = staging / ('nightly-' + token + '.db')
                receipt['candidate'] = str(candidate)
                stage = 'build'
                t = time.monotonic()
                try:
                    built = build(old, candidate, vault, **settings)
                finally:
                    receipt['seconds']['build'] = time.monotonic() - t
                if built.get('action') != 'activation_required' or status(candidate, vault)['state'] != 'fresh':
                    raise ValueError('candidate_not_fresh')
                conn, identity = open_snapshot(candidate)
                conn.close()
                stage = 'eval'
                t = time.monotonic()
                if gated is not None:
                    reasons = _suite_eval(receipt, old, candidate, gated, receipts, token,
                                          suite_evaluate, build_settings)
                    receipt['seconds']['eval'] = time.monotonic() - t
                else:
                    a, b, evidence = evaluate(old, candidate, gate_set, private_runner)
                    receipt['seconds']['eval'] = time.monotonic() - t
                    if 'gold_drift' in evidence:
                        receipt['gold_drift'] = report_only(evidence['gold_drift'])
                        evidence = {**evidence, 'gold_drift': receipt['gold_drift']}
                    mode = gate_mode(old, candidate, allow_index_contract_rebase=build_settings.get(
                        'allow_index_contract_rebase', False))
                    reasons, lost, excused = gate(
                        a, b, mode=mode,
                        note_changes=note_changes(old, candidate) if mode == 'content' else None)
                    receipt.update(gate=GATES[mode], gate_mode=mode,
                                   current_metrics=aggregates(a), candidate_metrics=aggregates(b),
                                   eval=evidence, lost_gold_ids=lost,
                                   excused_lost_gold=excused, reasons=reasons)
                stage = 'recheck'
                if pointer.resolve(strict=True) != old or sha(old) != old_id:
                    raise ValueError('source_changed')
                if status(candidate, vault)['state'] != 'fresh' or sha(candidate) != identity:
                    raise ValueError('candidate_changed')
                state_path = pointer.with_name(pointer.name + '.refresh-state.json')
                state = json.loads(state_path.read_text()) if state_path.exists() else {}
                if reasons:
                    receipt['outcome'] = 'HOLD'
                    state['last_held'] = str(candidate)
                    _json_write(state_path, state, exclusive=False)
                else:
                    stage = 'switch'
                    # Do not let an alarm land between atomic replacement and
                    # recording ownership needed for rollback.
                    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
                    try:
                        replace_pointer(pointer, candidate)
                        switched = True
                    finally:
                        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
                    receipt['new'] = {'db': str(candidate), 'snapshot_id': identity}
                    stage = 'self_check'
                    t = time.monotonic()
                    receipt['self_check'] = self_check(pointer, identity)
                    receipt['seconds']['self_check'] = time.monotonic() - t
                    if pointer.resolve(strict=True) != candidate:
                        raise ValueError('pointer_changed')
                    receipt['outcome'] = 'SWITCH'
                    state['previous'] = str(old)
                    _json_write(state_path, state, exclusive=False)
                    stage = 'retention'
                    receipt['removed'] = retention(staging, pointer, old, state.get('last_held'))
        except (Exception, DeadlineReached) as error:
            receipt['outcome'] = 'INCOMPLETE' if isinstance(error, DeadlineReached) else 'FAIL'
            receipt['reasons'] = ['internal_deadline'] if isinstance(error, DeadlineReached) else [stage + '_failed']
            if isinstance(error, ProbeError):
                receipt['self_check_error'] = {'code': error.code, 'class': error.error_class}
            if isinstance(error, SuiteGateError):
                receipt['reasons'] = [error.code]
            if isinstance(error, ExcessiveDrift):
                receipt['reasons'] = ['gold_drift_excessive']
                receipt['gold_drift'] = report_only(error.report)
            if switched:
                # Roll back only our own switch; never overwrite an unrelated external one.
                try:
                    if pointer.resolve(strict=True) == candidate:
                        replace_pointer(pointer, old)
                        receipt['rollback'] = 'restored_previous'
                    else:
                        receipt['rollback'] = 'external_pointer_change_not_overwritten'
                except Exception:
                    receipt['rollback'] = 'failed_requires_operator'
        # Quality follows the final pointer (SWITCH, HOLD, SKIP, or rollback),
        # independent of whether freshness required a candidate build today.
        try:
            if receipt['outcome'] == 'INCOMPLETE':
                raise DeadlineReached()
            if suite is not None:
                # The suite's quality sets are reported with the gate, on build days only.
                raise SuiteQualityNotMeasured()
            served = pointer.resolve(strict=True)
            conn, served_id = open_snapshot(served)
            conn.close()
            receipt['serving_quality'] = daily_quality(
                served, served_id, receipts, gate_set, private_runner,
                evaluator=quality_evaluate, quality_set=quality_set)
            if pointer.resolve(strict=True) != served or sha(served) != served_id:
                receipt['serving_quality'] = {'status': 'unavailable',
                                              'reason': 'serving_snapshot_changed'}
        except DeadlineReached:
            # A completed switch/hold is not undone for optional measurement.
            if receipt['outcome'] == 'SKIP':
                receipt.update(outcome='INCOMPLETE', reasons=['internal_deadline'])
            receipt['serving_quality'] = {'status': 'deferred', 'reason': 'internal_deadline'}
        except SuiteQualityNotMeasured:
            receipt['serving_quality'] = {'status': 'not_measured', 'reason': 'suite_gate'}
        except Exception:
            receipt['serving_quality'] = {'status': 'unavailable',
                                          'reason': 'serving_snapshot_unavailable'}
        # No alarm may interrupt the small final receipt write/fsync.
        signal.setitimer(signal.ITIMER_REAL, 0)
        receipt['seconds']['total'] = time.monotonic() - start
        receipt['recorded_at'] = datetime.now(UTC).isoformat().replace('+00:00', 'Z')
        _json_write(receipt_path, receipt)
        receipt['receipt'] = str(receipt_path)
        if suite is not None:
            try:
                prune_suite_runs(receipts)
            except OSError:
                pass  # retention is best effort; the receipt is already written
        return receipt


class SuiteQualityNotMeasured(Exception):
    """Control flow only: the suite gate has no separate daily serving-quality run."""


def _suite_eval(receipt, old, candidate, suite, receipts, token, suite_evaluate, build_settings):
    """The suite gate's eval stage: score, compare, fill the receipt; returns the reasons."""
    mode = gate_mode(old, candidate, allow_index_contract_rebase=build_settings.get(
        'allow_index_contract_rebase', False))
    changes = note_changes(old, candidate) if mode == 'content' else None
    runs = receipts / SUITE_RUNS
    runs.mkdir(mode=0o700, exist_ok=True)
    receipt['suite']['runs'] = SUITE_RUNS + '/' + token
    comparison, evidence = suite_evaluate(old, candidate, suite, runs / token, mode=mode,
                                          note_changes=changes)
    headlines = evidence.pop('headlines', {})
    if 'gold_drift' in evidence:
        receipt['gold_drift'] = report_only(evidence['gold_drift'])
        evidence = {**evidence, 'gold_drift': receipt['gold_drift']}
    gate_info = {**SUITE_GATE, 'mode': mode}
    if mode == 'content':
        gate_info.update(content_max_drop=CONTENT_TOLERANCE,
                         lost_gold_excused=CONTENT_GATE['lost_all_gold_excused'])
    receipt.update(gate=gate_info, gate_mode=mode, eval=evidence)
    receipt['suite'].update(result=comparison.get('result'), problems=comparison.get('problems', []))
    reasons, lost, excused, report = suite_gate(comparison)
    for sid, part in report.items():
        part.update(current=headlines.get('current', {}).get(sid),
                    candidate=headlines.get('candidate', {}).get(sid))
    receipt['suite']['sets'] = report
    receipt.update(lost_gold_ids=lost, excused_lost_gold=excused, reasons=reasons)
    return reasons


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='quoth refresh-nightly',
        description='Build a successor snapshot when the vault changed, score it against the'
                    ' serving snapshot on the gate (--suite, or the legacy --gate-set with'
                    ' --private-runner), and switch the serving pointer when it passes. Prints'
                    ' one status line; the full receipt is written to --receipts.',
        epilog='exit codes: 0 serving is fresh, 1 runtime failure, 2 usage error,'
               ' 3 successor held back, run incomplete, or heartbeat not green')
    parser.add_argument('--pointer', type=Path, required=True,
                        help='serving pointer: a symlink to the snapshot being served')
    parser.add_argument('--vault', type=Path, required=True, help='vault directory')
    parser.add_argument('--staging', type=Path, required=True,
                        help='directory for successor snapshots and their checkpoints')
    parser.add_argument('--receipts', type=Path, required=True,
                        help='directory the run receipts are written to')
    # A gate is required: --suite, or the legacy --gate-set with --private-runner. Its sets
    # must be private sets written against the real vault: the wheel only carries fixture
    # sets, whose paths do not exist in a real vault.
    parser.add_argument('--suite', type=Path,
                        help='eval suite (quoth-evalsuite/v1) the gate scores both snapshots on:'
                             ' smoke/regression sets block on their tolerances, quality sets on a'
                             ' significantly worse paired test, tune sets are skipped; replaces'
                             ' --gate-set, --private-runner and --quality-set')
    parser.add_argument('--suite-fixture', default=None,
                        help=f'the fixture the suite must declare (default {SUITE_FIXTURE!r}, what'
                             ' `eval convert` writes for sets of your own vault)')
    parser.add_argument('--gate-set', type=Path,
                        help='legacy gate: eval set (JSONL + manifest) the gate scores both'
                             ' snapshots on; keep it private, next to the install')
    parser.add_argument('--private-runner', type=Path,
                        help='legacy gate, deprecated: private gate scorer module (validation and'
                             ' metrics functions), executed as Python')
    parser.add_argument('--deadline-seconds', type=float, default=3000,
                        help='internal wall-clock budget, >0 and <=3000; preserves receipt/checkpoint')
    parser.add_argument('--quality-set', type=Path,
                        help='legacy gate: optional eval set for daily serving quality (the'
                             ' golden_v3 block); timed separately')
    parser.add_argument('--heartbeat', type=Path,
                        help='heartbeat JSON written after every run (default: <receipts>/../heartbeat.json)')
    parser.add_argument('--allow-index-contract-rebase', action='store_true',
                        help='one operator-run successor across a declared semantics-preserving'
                             ' index-code change; never set this in the scheduled job')
    args = parser.parse_args(argv)
    if args.suite is not None:
        if args.gate_set or args.private_runner or args.quality_set:
            parser.error('--suite replaces --gate-set, --private-runner and --quality-set:'
                         ' give one or the other')
    else:
        if args.suite_fixture is not None:
            parser.error('--suite-fixture needs --suite')
        if args.gate_set is None or args.private_runner is None:
            parser.error('a gate is required: --suite, or --gate-set with --private-runner')
    if args.private_runner is not None and sys.stderr.isatty():
        # Only for an operator at a terminal: a scheduled run stays silent (cron mails any
        # output); every receipt records the deprecation instead.
        print(DEPRECATED_RUNNER, file=sys.stderr)
    suite_gate_run = args.suite is not None
    settings = dict(vars(args))
    heartbeat_path = settings.pop('heartbeat') or args.receipts.absolute().parent / 'heartbeat.json'
    if not settings['allow_index_contract_rebase']:
        settings.pop('allow_index_contract_rebase')
    for key in ('suite', 'suite_fixture', 'quality_set'):
        if settings[key] is None:
            settings.pop(key)
    if suite_gate_run:
        settings.pop('gate_set')
        settings.pop('private_runner')
    try:
        # Suppress underlying progress text, never leak queries or note paths to cron.
        with redirect_stdout(io.StringIO()):
            result = run(**settings)
    except Exception:
        print('FAIL quoth-nightly runner_failed')
        result = {'outcome': 'FAIL'}
    if result['outcome'] == 'BUSY':
        return 0  # the run holding the lock writes the heartbeat
    try:
        beat = write_heartbeat(heartbeat_path, check(args.pointer, args.receipts))
    except Exception:
        print('FAIL quoth-nightly heartbeat_unavailable')
        return 1
    if result['outcome'] not in ('SKIP', 'BUSY') and 'receipt' in result:
        print(f"{result['outcome']} quoth-nightly receipt={result['receipt']}")
    if beat['status'] != 'green':
        print('RED quoth-heartbeat ' + ','.join(beat['reasons']))
    # Non-zero whenever serving is not demonstrably fresh, so the scheduler sees it.
    return {'FAIL': 1, 'HOLD': 3, 'INCOMPLETE': 3}.get(result['outcome'], 0 if beat['status'] == 'green' else 3)
