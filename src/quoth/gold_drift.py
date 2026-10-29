"""Bounded identity-only gold aliases; no note paths/text in public reports."""
from __future__ import annotations

from collections import defaultdict
from contextlib import closing
import json
from pathlib import Path
import re

from .db import connect_read_only
from .refresh import db_inventory, inventory

TOKEN = re.compile(r'[A-Za-z0-9._-]{1,128}')


class ExcessiveDrift(ValueError):
    def __init__(self, report):
        self.report = report
        super().__init__('gold_drift_excessive')


def report_only(report):
    """Closed receipt surface, including for cached/untrusted evaluator evidence."""
    out = {k: report[k] for k in ('renamed', 'stale')}
    if any(type(v) is not int or v < 0 for v in out.values()):
        raise ValueError('gold_drift_invalid')
    ids = report['ids']
    if (not isinstance(ids, list) or any(not isinstance(q, str) or not TOKEN.fullmatch(q) for q in ids)
            or ids != sorted(set(ids))):
        raise ValueError('gold_ids_invalid')
    return {**out, 'ids': ids}


def resolve(datasets, old, candidate):
    """Resolve ONCE for both sides. Partial-gold loss excludes the whole item.

    Counts are per dataset item; IDs are the unique affected IDs across datasets.
    Existing paths are never excluded for changed text (that is a real regression).
    """
    by_hash = defaultdict(list)
    for path, identity in candidate.items():
        by_hash[identity].append(path)
    anchored, aliases = {}, {}
    report = {'renamed': 0, 'stale': 0, 'ids': []}
    affected = set()
    excessive = False
    for name, rows in datasets.items():
        ids = [q['id'] for q in rows]
        if (len(set(ids)) != len(ids) or any(not isinstance(q, str) or not TOKEN.fullmatch(q) for q in ids)):
            raise ValueError('gold_ids_invalid')
        kept, stale = [], 0
        for row in rows:
            paths, mapped = [], {}
            for path in row['expected_paths']:
                if Path(path).is_absolute() or '..' in Path(path).parts:
                    raise ValueError('gold_path_invalid')
                if path in candidate:
                    paths.append(path)
                else:
                    matches = by_hash.get(old.get(path), []) if path in old else []
                    if len(matches) != 1:
                        break
                    paths.append(matches[0])
                    mapped[path] = matches[0]
            if (len(paths) != len(row['expected_paths'])
                    or len(set(paths)) != len(set(row['expected_paths']))):
                stale += 1
                report['stale'] += 1
                affected.add(row['id'])
                continue
            if mapped:
                report['renamed'] += 1
                affected.add(row['id'])
                aliases.update(mapped)
            kept.append({**row, 'expected_paths': paths})
        anchored[name] = kept
        excessive |= stale * 10 > len(rows) or (stale > 0 and len(kept) < 30)
    report['ids'] = sorted(affected)
    if excessive:
        raise ExcessiveDrift(report)
    return anchored, aliases, report


def canonical_hits(hits, aliases, identities=None):
    """Scorer-only copies; preserve order, text, score and evidence consumption."""
    collisions = set()
    if identities is not None:
        collisions = {target for origin, target in aliases.items()
                      if target in identities and identities[target] != identities.get(origin)}
    # A rename may replace an unrelated note at its destination. The old
    # destination is not that identity and cannot earn a hit for the renamed gold.
    return [{**hit, 'path': ('\0' if hit['path'] in collisions else
                            aliases.get(hit['path'], hit['path']))} for hit in hits]


def snapshot_inventory(db):
    with closing(connect_read_only(db)) as conn:
        return db_inventory(conn)


def snapshot_identities(db):
    """Latest stored identity per path, including retired renamed/deleted paths.

    A switched successor preserves historical versions needed to resolve frozen
    gold. Select latest identity per path, never guess among different versions.
    """
    with closing(connect_read_only(db)) as conn:
        return dict(conn.execute(
            "SELECT s.path, v.content_hash FROM sources s JOIN source_versions v "
            "ON v.source_id=s.id WHERE v.id=(SELECT max(v2.id) "
            "FROM source_versions v2 WHERE v2.source_id=s.id)"))


def daily_source(db):
    """Live candidate source plus identities in the served frozen snapshot."""
    manifest = json.loads(db.with_suffix('.manifest.json').read_text())
    vault = Path(manifest['vault_root'])
    return snapshot_identities(db), inventory(vault), vault
