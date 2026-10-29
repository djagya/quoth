"""Decision 2A: content-only refresh gate vs strict code gate (synthetic fixtures)."""
import json
from pathlib import Path

from quoth import nightly
from quoth.freshness import successor
from quoth.refresh import index_code_digests
from test_nightly import setup
from test_refresh import FakeEmbedder


def metrics(hit=1.0, mrr=1.0, recall=1.0, items=None, gold=None):
    items = items if items is not None else {'q1': True, 'q2': True}
    gold = gold if gold is not None else {'q1': ['a.md'], 'q2': ['deleted.md']}
    return {d: {'n': len(items), 'hit@10': hit, 'mrr@10': mrr, 'recall@10': recall,
                'items': dict(items), 'gold': {k: list(v) for k, v in gold.items()}}
            for d in ('private', 'synthetic')}


def content_setup(tmp_path):
    """Serving snapshot whose manifest carries the current index-code digests."""
    src, vault, pointer = setup(tmp_path)
    path = src.with_suffix('.manifest.json')
    data = json.loads(path.read_text())
    data['index_code_digests'] = index_code_digests()
    path.write_text(json.dumps(data))
    return src, vault, pointer


def run(tmp_path, current, candidate, content=True):
    src, vault, pointer = content_setup(tmp_path)
    receipt = nightly.run(pointer=pointer, vault=vault, staging=tmp_path / 'staging',
                          receipts=tmp_path / 'receipts', gate_set=tmp_path / 'golden.jsonl',
                          private_runner=tmp_path / 'runner.py', embedder=FakeEmbedder(),
                          evaluate=lambda *a: (current, candidate, {}),
                          self_check=lambda *a: {'synthetic_fixture': True})
    return receipt, src, pointer


# ---- pure gate ---------------------------------------------------------------

def test_content_tolerance_boundary_and_code_strictness():
    a = metrics()
    b = metrics(hit=0.97, mrr=0.97, recall=0.97)
    assert nightly.gate(a, b, mode='content')[0] == []
    assert sorted(nightly.gate(a, b, mode='code')[0]) == [
        'private_hit@10_regressed', 'private_mrr@10_regressed', 'synthetic_recall@10_regressed']
    b = metrics(hit=0.969, mrr=0.97, recall=0.969)
    assert nightly.gate(a, b, mode='content')[0] == [
        'private_hit@10_regressed', 'synthetic_recall@10_regressed']


def test_lost_gold_excused_only_for_changed_or_removed_notes():
    a = metrics()
    b = metrics(items={'q1': False, 'q2': False})
    changes = {'a.md': nightly.GOLD_NOTE_CHANGED, 'deleted.md': nightly.GOLD_NOTE_REMOVED}
    reasons, lost, excused = nightly.gate(a, b, mode='content', note_changes=changes)
    assert lost == {'private': ['q1', 'q2'], 'synthetic': ['q1', 'q2']}
    assert excused['private'] == [{'id': 'q1', 'reason': 'gold_note_changed'},
                                  {'id': 'q2', 'reason': 'gold_note_removed'}]
    assert not any('lost_all_gold' in r for r in reasons)
    # One gold note untouched: that item still blocks.
    reasons, _, excused = nightly.gate(a, b, mode='content',
                                       note_changes={'a.md': nightly.GOLD_NOTE_CHANGED})
    assert [e['id'] for e in excused['synthetic']] == ['q1']
    assert 'private_lost_all_gold' in reasons and 'synthetic_lost_all_gold' in reasons
    # Code mode never excuses.
    reasons, _, excused = nightly.gate(a, b, mode='code', note_changes=changes)
    assert excused == {'private': [], 'synthetic': []}
    assert 'private_lost_all_gold' in reasons


def test_gate_mode_requires_identical_index_digests(tmp_path):
    old, new = tmp_path / 'old.db', tmp_path / 'new.db'
    digests = {'chunker.py': 'x'}

    def write(db, value):
        db.with_suffix('.manifest.json').write_text(json.dumps(
            {} if value is None else {'index_code_digests': value}))
    write(old, digests)
    write(new, dict(digests))
    assert nightly.gate_mode(old, new) == 'content'
    write(new, {'chunker.py': 'y'})
    assert nightly.gate_mode(old, new) == 'code'
    # The rebase flag excuses only the declared semantics-preserving files.
    assert nightly.gate_mode(old, new, allow_index_contract_rebase=True) == 'code'
    write(old, {'chunker.py': 'x', 'refresh.py': 'a'})
    write(new, {'chunker.py': 'x', 'refresh.py': 'b', 'embed/store.py': 'c'})
    assert nightly.gate_mode(old, new) == 'code'
    assert nightly.gate_mode(old, new, allow_index_contract_rebase=True) == 'content'
    # Legacy manifests: code_digests only; serving-only search.py never decides the mode.
    old.with_suffix('.manifest.json').write_text(json.dumps(
        {'code_digests': {'chunker.py': 'x', 'search.py': 's1'}}))
    new.with_suffix('.manifest.json').write_text(json.dumps(
        {'code_digests': {'chunker.py': 'x', 'search.py': 's2'}, 'index_code_digests': {'x': 'y'}}))
    assert nightly.gate_mode(old, new) == 'content'
    write(old, None)
    write(new, None)
    assert nightly.gate_mode(old, new) == 'code'


# ---- end to end through nightly.run ----------------------------------------

def test_content_only_small_drop_switches(tmp_path):
    r, src, pointer = run(tmp_path, metrics(), metrics(hit=0.98, mrr=0.975, recall=0.97))
    assert r['outcome'] == 'SWITCH', r['reasons']
    assert r['gate_mode'] == 'content'
    assert r['gate']['private_hit10_drop'] == nightly.CONTENT_TOLERANCE
    assert pointer.resolve() == Path(r['candidate'])
    text = Path(r['receipt']).read_text()
    assert 'gold' not in json.loads(text)['current_metrics']['private']
    assert 'a.md' not in text and 'deleted.md' not in text


def test_content_only_large_drop_holds(tmp_path):
    r, src, pointer = run(tmp_path, metrics(), metrics(hit=0.96))
    assert r['outcome'] == 'HOLD'
    assert r['gate_mode'] == 'content'
    assert r['reasons'] == ['private_hit@10_regressed']
    assert pointer.resolve() == src


def test_code_change_small_drop_holds(tmp_path, monkeypatch):
    # Preflight already refuses undeclared index-code changes, so a run reaches the
    # gate in code mode only when gate_mode() cannot prove content-only; force that.
    monkeypatch.setattr(nightly, 'gate_mode', lambda *a, **k: 'code')
    r, src, pointer = run(tmp_path, metrics(), metrics(hit=0.99))
    assert r['outcome'] == 'HOLD'
    assert r['gate_mode'] == 'code'
    assert r['reasons'] == ['private_hit@10_regressed']
    assert pointer.resolve() == src


def test_lost_gold_on_changed_note_is_excused(tmp_path):
    # setup() changes a.md; q1's gold is a.md.
    current = metrics(hit=1.0, items={f'q{i}': True for i in range(1, 41)},
                      gold={**{f'q{i}': ['deleted.md'] for i in range(2, 41)}, 'q1': ['a.md']})
    lost = {**current['private']['items'], 'q1': False}
    candidate = metrics(hit=0.975, mrr=0.975, recall=0.975, items=lost,
                        gold=current['private']['gold'])
    r, src, pointer = run(tmp_path, current, candidate)
    assert r['outcome'] == 'SWITCH', r['reasons']
    assert r['lost_gold_ids'] == {'private': ['q1'], 'synthetic': ['q1']}
    assert r['excused_lost_gold'] == {
        d: [{'id': 'q1', 'reason': 'gold_note_changed'}] for d in ('private', 'synthetic')}


def test_lost_gold_on_unchanged_note_holds(tmp_path):
    # deleted.md is untouched by setup(); q2's gold points at it.
    current = metrics(hit=1.0, items={f'q{i}': True for i in range(1, 41)},
                      gold={**{f'q{i}': ['a.md'] for i in range(1, 41)}, 'q2': ['deleted.md']})
    lost = {**current['private']['items'], 'q2': False}
    candidate = metrics(hit=0.975, mrr=0.975, recall=0.975, items=lost,
                        gold=current['private']['gold'])
    r, src, pointer = run(tmp_path, current, candidate)
    assert r['outcome'] == 'HOLD'
    assert r['gate_mode'] == 'content'
    assert r['reasons'] == ['private_lost_all_gold', 'synthetic_lost_all_gold']
    assert r['excused_lost_gold'] == {'private': [], 'synthetic': []}
    assert pointer.resolve() == src


def test_note_changes_reports_changed_removed_not_renamed(tmp_path):
    src, vault, pointer = content_setup(tmp_path)
    (vault / 'deleted.md').unlink()
    candidate = tmp_path / 'candidate.db'
    successor(src, candidate, vault, embedder=FakeEmbedder())
    assert nightly.note_changes(src, candidate) == {
        'a.md': 'gold_note_changed', 'deleted.md': 'gold_note_removed'}
    assert nightly.gate_mode(src, candidate) == 'content'
    renamed = tmp_path / 'renamed.db'
    (vault / 'moved.md').write_text((vault / 'a.md').read_text())
    (vault / 'a.md').unlink()
    successor(candidate, renamed, vault, embedder=FakeEmbedder())
    assert nightly.note_changes(candidate, renamed) == {}
