"""Identity-only gold resolution, whole-item symmetric exclusion and privacy."""
import json

import pytest

from quoth.gold_drift import ExcessiveDrift, resolve, canonical_hits


def rows(n=40):
    return [{'id': f'q{i}', 'question': 'synthetic question', 'kind': 'ordinary',
             'expected_paths': ['kept.md']} for i in range(n)]


def test_rename_is_identity_only_and_maps_both_rankings():
    gold = rows()
    gold[0]['expected_paths'] = ['old.md']
    anchored, aliases, report = resolve({'private': gold},
        {'old.md': 'identity', 'kept.md': 'kept'},
        {'new.md': 'identity', 'kept.md': 'changed'})
    assert anchored['private'][0]['expected_paths'] == ['new.md']
    assert gold[0]['expected_paths'] == ['old.md']
    old_hits = [{'path': 'old.md', 'text': 'do not modify', 'score': 1}]
    new_hits = [{'path': 'new.md', 'text': 'do not modify', 'score': 1}]
    assert canonical_hits(old_hits, aliases) == canonical_hits(new_hits, aliases)
    assert old_hits[0]['path'] == 'old.md'
    assert report == {'renamed': 1, 'stale': 0, 'ids': ['q0']}


@pytest.mark.parametrize('candidate', [
    {'kept.md': 'kept'},
    {'kept.md': 'kept', 'new.md': 'identity', 'duplicate.md': 'identity'},
    {'kept.md': 'kept', 'new.md': 'different'},
])
def test_deletion_ambiguity_and_modified_rename_exclude_entire_item(candidate):
    gold = rows()
    gold[0]['expected_paths'] = ['old.md', 'kept.md']
    anchored, aliases, report = resolve({'private': gold, 'synthetic': gold},
        {'old.md': 'identity', 'kept.md': 'kept'}, candidate)
    assert all(len(v) == 39 and 'q0' not in {q['id'] for q in v} for v in anchored.values())
    assert report == {'renamed': 0, 'stale': 2, 'ids': ['q0']}
    assert not aliases
    assert 'old.md' not in json.dumps(report) and 'synthetic question' not in json.dumps(report)


@pytest.mark.parametrize('n,stale', [(40, 5), (30, 1), (1, 1)])
def test_excessive_fraction_or_floor_fails_closed(n, stale):
    gold = rows(n)
    for q in gold[:stale]:
        q['expected_paths'] = ['missing.md']
    with pytest.raises(ExcessiveDrift, match='gold_drift_excessive') as e:
        resolve({'private': gold}, {'kept.md': 'kept'}, {'kept.md': 'kept'})
    assert e.value.report['stale'] == stale
    assert 'missing.md' not in str(e.value)


def test_exact_ten_percent_allowed_and_missing_old_hash_not_guessed():
    gold = rows()
    for q in gold[:4]:
        q['expected_paths'] = ['unknown.md']
    anchored, _, report = resolve({'synthetic': gold}, {}, {'kept.md': 'kept'})
    assert len(anchored['synthetic']) == 36 and report['stale'] == 4


def test_ids_closed_allowlist_and_duplicate_rows_fail():
    gold = rows()
    gold[0]['id'] = '/private/note.md'
    with pytest.raises(ValueError, match='gold_ids_invalid'):
        resolve({'private': gold}, {}, {})
    gold = rows()
    gold[1]['id'] = gold[0]['id']
    with pytest.raises(ValueError, match='gold_ids_invalid'):
        resolve({'private': gold}, {}, {})


def test_rename_destination_does_not_credit_unrelated_old_content():
    aliases = {'old.md': 'new.md'}
    old = {'old.md': 'gold-identity', 'new.md': 'unrelated-identity'}
    hits = [{'path': 'new.md', 'text': 'unrelated'}, {'path': 'old.md', 'text': 'gold'}]
    scored = canonical_hits(hits, aliases, old)
    assert scored[0]['path'] != 'new.md' and scored[1]['path'] == 'new.md'
    assert [h['text'] for h in scored] == [h['text'] for h in hits]
    assert canonical_hits(hits[:1], aliases, {'old.md': 'gold', 'new.md': 'gold'})[0]['path'] == 'new.md'


def test_gold_paths_collapsing_under_rename_are_stale_not_partial_gold():
    gold = rows()
    gold[0]['expected_paths'] = ['old.md', 'new.md']
    anchored, _, report = resolve({'private': gold},
        {'old.md': 'gold', 'new.md': 'different'}, {'new.md': 'gold', 'kept.md': 'kept'})
    assert len(anchored['private']) == 39 and report['stale'] == 1
