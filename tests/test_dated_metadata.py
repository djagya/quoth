"""Default-only dated metadata, salvaged from T1; synthetic fixtures, no ONNX."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from quoth.db import connect_read_only, open_db
from quoth.dated import _DATE_CACHE, build_evidence, date_index, drop_cache, note_date
from quoth.embed.backends import get_embedder
from quoth.embed.store import embed_chunks
from quoth.evidence import evidence_bundle
from quoth.ingest import ingest_vault
from quoth.refresh import sha
from quoth.search import search
from quoth.serving import ServingSnapshot, replace_pointer


@pytest.mark.parametrize('path,metadata,expected', [
    ('A/2026-09-24.md', '{"created":"2026-09-24"}', ('2026-09-24', 'validated_note_date')),
    ('A/2026-09-24 — title.md', '{"created":"2026-09-24T10:00"}', ('2026-09-24', 'validated_note_date')),
    ('A/2026-09-24.md', '{"created":"2026-09-25"}', (None, 'filename_only')),
    ('A/2026-09-24.md', None, (None, 'filename_only')),
    ('A/2026-09-24.md', 'not json', (None, 'filename_only')),
    ('A/2026-09-24.md', '[]', (None, 'filename_only')),
    ('A/notes.md', '{"created":"2026-09-24"}', (None, 'none')),
    ('A/2026-02-30.md', '{"created":"2026-02-30"}', (None, 'none')),
])
def test_note_date_basis_rules(path, metadata, expected):
    assert note_date(path, metadata) == expected


@pytest.fixture
def jdb(tmp_path):
    vault = tmp_path / 'v'
    vault.mkdir()
    for filename, created in [('2026-09-01.md', '2026-09-01'),
                              ('2026-09-03.md', '2026-09-02')]:
        (vault / filename).write_text(f'---\ncreated: {created}\n---\n# Journal\n\nWalked the dog in the park.\n')
    (vault / 'Dog notes.md').write_text('# Dog notes\n\nThe dog likes long walks.\n')
    db = tmp_path / 'j.db'
    conn = open_db(db)
    ingest_vault(conn, vault)
    embed_chunks(conn, get_embedder('hash-ngram'))
    conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    conn.execute('PRAGMA journal_mode=DELETE')
    conn.close()
    return db


def test_default_dense_payload_unchanged(jdb):
    conn = connect_read_only(jdb)
    try:
        emb = get_embedder('hash-ngram')
        legacy = evidence_bundle(search(conn, 'dog walk park', k=3, retriever='dense',
                                        model='hash-ngram', embedder=emb), k=3)
        bundle = build_evidence(conn, 'dog walk park', k=3, model='hash-ngram', embedder=emb)
        assert len(bundle['hits']) == 3
        stripped = {**bundle, 'hits': [{k: v for k, v in h.items()
                                     if k not in ('note_date', 'date_basis')} for h in bundle['hits']]}
        assert json.dumps(stripped, ensure_ascii=False) == json.dumps(legacy, ensure_ascii=False)
        assert 'temporal' not in bundle
        dates = {h['path']: (h['note_date'], h['date_basis']) for h in bundle['hits']}
        assert dates == {'2026-09-01.md': ('2026-09-01', 'validated_note_date'),
                         '2026-09-03.md': (None, 'filename_only'), 'Dog notes.md': (None, 'none')}

    finally:
        drop_cache(conn)
        conn.close()


def test_cache_reuses_and_invalidates_own_and_external_metadata_writes(jdb):
    conn = open_db(jdb)
    other = open_db(jdb)
    try:
        initial = date_index(conn)
        assert date_index(conn) is initial
        conn.execute("UPDATE source_versions SET metadata='{}'")
        conn.commit()
        changed = date_index(conn)
        assert changed is not initial
        assert changed.by_path['2026-09-01.md'] == (None, 'filename_only')
        other.execute('UPDATE source_versions SET metadata=?', ('{"created":"2026-09-01"}',))
        other.commit()
        assert date_index(conn).by_path['2026-09-01.md'] == ('2026-09-01', 'validated_note_date')
        assert date_index(other) is not date_index(conn)
        drop_cache(conn)
        assert conn not in _DATE_CACHE
    finally:
        drop_cache(conn)
        drop_cache(other)
        conn.close()
        other.close()


def test_cache_bounded_and_current_versions_only(jdb):
    connections = [connect_read_only(jdb) for _ in range(9)]
    try:
        for conn in connections:
            date_index(conn)
        assert len(_DATE_CACHE) <= 8
        assert connections[0] not in _DATE_CACHE
    finally:
        for conn in connections:
            drop_cache(conn)
            conn.close()
    conn = open_db(jdb)
    try:
        conn.execute('UPDATE source_versions SET is_current=0')
        conn.commit()
        assert date_index(conn).by_path == {}
    finally:
        drop_cache(conn)
        conn.close()


def _manifest(db):
    db.with_suffix('.manifest.json').write_text(json.dumps({
        'db': str(db), 'db_sha256': sha(db), 'db_bytes': db.stat().st_size,
        'status': 'frozen_current_vault_candidate', 'integrity': 'ok',
        'schema': 'validate_evidence_db'}))


def test_snapshot_replace_and_close_drop_metadata_cache(jdb, tmp_path):
    import sqlite3
    second = tmp_path / 'second.db'
    with sqlite3.connect(jdb) as source, sqlite3.connect(second) as target:
        source.backup(target)
        target.execute("UPDATE source_versions SET metadata='{}'")
    source.close()
    target.close()
    for db in (jdb, second):
        _manifest(db)
    pointer = tmp_path / 'serving.db'
    pointer.symlink_to(jdb)
    serving = ServingSnapshot(pointer)
    try:
        with serving.lease() as (old, identity):
            assert date_index(old).by_path['2026-09-01.md'][0] == '2026-09-01'
        replace_pointer(pointer, second)
        with serving.lease() as (new, identity):
            assert old not in _DATE_CACHE
            assert date_index(new).by_path['2026-09-01.md'] == (None, 'filename_only')
        pointer.unlink()
        pointer.symlink_to(tmp_path / 'missing.db')
        with serving.lease() as (active, _):
            assert active is new and active in _DATE_CACHE
    finally:
        serving.close()
    assert new not in _DATE_CACHE


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_mcp_default_only_dated_hits(jdb, tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    before = jdb.read_bytes()
    params = StdioServerParameters(
        command=sys.executable,
        args=['-m', 'quoth.cli', 'mcp', '--db', str(jdb)],
        cwd=str(tmp_path), env={'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            assert {t.name for t in tools} == {'evidence_bundle'}
            assert set(tools[0].input_schema['properties']) == {'query', 'k', 'model'}
            result = await session.call_tool('evidence_bundle', {
                'query': 'dog walk park', 'k': 3, 'model': 'hash-ngram'})
            assert not result.is_error
            bundle = json.loads(''.join(c.text for c in result.content if hasattr(c, 'text')))
            assert 'temporal' not in bundle
            assert bundle['provenance']['snapshot_id'] == sha(jdb)
            assert len(bundle['hits']) == 3
            dates = {h['path']: (h['note_date'], h['date_basis']) for h in bundle['hits']}
            assert dates['2026-09-01.md'] == ('2026-09-01', 'validated_note_date')
            assert dates['2026-09-03.md'] == (None, 'filename_only')
            assert dates['Dog notes.md'] == (None, 'none')
            assert all(h['provenance']['snapshot_id'] == sha(jdb) for h in bundle['hits'])
    assert jdb.read_bytes() == before

