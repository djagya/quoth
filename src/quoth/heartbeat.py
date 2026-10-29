"""Nightly/serving heartbeat: one small JSON status a host monitor can alarm on.

Green only when the serving snapshot was confirmed fresh (a SWITCH or SKIP
receipt) within max_age_hours, the newest run is recent, and it did not end in
HOLD, INCOMPLETE or FAIL. Receipts and the pointer are read, never modified.
Contains outcomes, IDs and timestamps only: no questions, paths of notes or text.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys
import uuid

SCHEMA = 'quoth-heartbeat/v1'
MAX_FRESH_AGE_HOURS = 36.0
MAX_RUN_AGE_HOURS = 26.0
CONFIRMS_FRESH = ('SWITCH', 'SKIP')
RED_OUTCOMES = ('HOLD', 'INCOMPLETE', 'FAIL')


def _parse_time(value) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _hours(now: datetime, then: datetime | None) -> float | None:
    return None if then is None else round((now - then).total_seconds() / 3600, 2)


def _receipts(directory: Path) -> list[dict]:
    out = []
    for path in directory.glob('*.json'):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        when = _parse_time(data.get('recorded_at')) if isinstance(data, dict) else None
        if when is None or not isinstance(data.get('outcome'), str):
            continue
        out.append({'receipt_id': data.get('receipt_id', path.stem), 'outcome': data['outcome'],
                    'reasons': [r for r in data.get('reasons', []) if isinstance(r, str)],
                    'recorded_at': when})
    return sorted(out, key=lambda r: (r['recorded_at'], str(r['receipt_id'])))


def check(pointer: Path, receipts: Path, *, max_age_hours: float = MAX_FRESH_AGE_HOURS,
          max_run_age_hours: float = MAX_RUN_AGE_HOURS, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    reasons = []
    serving = None
    try:
        target = Path(pointer).resolve(strict=True)
        manifest = target.with_suffix('.manifest.json')
        frozen_at = datetime.fromtimestamp(manifest.stat().st_mtime, UTC)
        serving = {'db': str(target), 'frozen_at': frozen_at.isoformat().replace('+00:00', 'Z'),
                   'age_hours': _hours(now, frozen_at)}
    except OSError:
        reasons.append('serving_pointer_invalid')
    runs = _receipts(Path(receipts)) if Path(receipts).is_dir() else []
    last = runs[-1] if runs else None
    confirmed = next((r for r in reversed(runs) if r['outcome'] in CONFIRMS_FRESH), None)
    fresh_as_of = confirmed['recorded_at'] if confirmed else None
    if last is None:
        reasons.append('no_receipts')
    else:
        if _hours(now, last['recorded_at']) > max_run_age_hours:
            reasons.append('last_run_overdue')
        if last['outcome'] in RED_OUTCOMES:
            reasons.append('last_run_' + last['outcome'].lower())
    if fresh_as_of is None or _hours(now, fresh_as_of) > max_age_hours:
        reasons.append('freshness_unconfirmed')
    stamp = (lambda t: t.isoformat().replace('+00:00', 'Z'))
    return {
        'schema': SCHEMA, 'checked_at': stamp(now),
        'status': 'red' if reasons else 'green', 'reasons': reasons,
        'max_fresh_age_hours': max_age_hours, 'max_run_age_hours': max_run_age_hours,
        'fresh_as_of': stamp(fresh_as_of) if fresh_as_of else None,
        'fresh_age_hours': _hours(now, fresh_as_of),
        'serving': serving,
        'last_run': None if last is None else {
            'receipt_id': last['receipt_id'], 'outcome': last['outcome'], 'reasons': last['reasons'],
            'recorded_at': stamp(last['recorded_at']), 'age_hours': _hours(now, last['recorded_at'])},
    }


def write_heartbeat(path: Path, payload: dict) -> dict:
    """Atomic replace; readable by a host monitor (no private content inside)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(payload, out, indent=2, sort_keys=True)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return payload


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='quoth heartbeat', description=__doc__.splitlines()[0])
    parser.add_argument('--pointer', type=Path, required=True)
    parser.add_argument('--receipts', type=Path, required=True)
    parser.add_argument('--output', type=Path, help='also write the heartbeat JSON here')
    parser.add_argument('--max-age-hours', type=float, default=MAX_FRESH_AGE_HOURS)
    args = parser.parse_args(argv)
    payload = check(args.pointer, args.receipts, max_age_hours=args.max_age_hours)
    if args.output:
        write_heartbeat(args.output, payload)
    print(json.dumps(payload, sort_keys=True))
    return 0 if payload['status'] == 'green' else 1


if __name__ == '__main__':
    sys.exit(main())
