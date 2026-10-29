"""MCP stdio round-trip via the official SDK client against a fixture DB."""

from __future__ import annotations

import anyio
import json
from pathlib import Path
import sqlite3
import sys

import pytest

from quoth.ingest import ingest_vault

mcp = pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402


def _params(db_path, tmp_path):
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "quoth.cli", "mcp", "--db", str(db_path)],
        cwd=str(tmp_path),
        # The SDK sanitizes its subprocess environment. Pin this worktree,
        # rather than accidentally testing a canonical editable installation.
        env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )


def _text(result) -> str:
    assert result.content, result
    blocks = [c.text for c in result.content if hasattr(c, "text")]
    return "".join(blocks)


@pytest.fixture
def db(vault, db_path, tmp_path):
    from quoth.db import open_db
    from quoth.embed.backends import get_embedder
    from quoth.embed.store import embed_chunks

    conn = open_db(db_path)
    ingest_vault(conn, vault)
    embed_chunks(conn, get_embedder("hash-ngram"), batch_size=8)
    conn.close()
    return db_path


@pytest.mark.anyio
async def test_mcp_round_trip(db, tmp_path):
    async with stdio_client(_params(db, tmp_path)) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            assert init.server_info.name == "quoth"

            (tool,) = (await session.list_tools()).tools
            assert tool.name == "evidence_bundle"
            assert tool.annotations.read_only_hint is True
            props = tool.input_schema["properties"]
            assert props["k"]["minimum"] == 1 and props["k"]["maximum"] == 20
            assert props["query"]["maxLength"] == 2048 and props["query"]["minLength"] == 1
            assert "hash-ngram" in props["model"]["enum"]
            assert all(props[name].get("description")
                       for name in ("query", "k", "model", "path_prefix", "path", "include_explicit"))
            assert "ambiguous" in props["path_prefix"]["description"]
            assert "owner asks" in props["include_explicit"]["description"]

            r = await session.call_tool("evidence_bundle", {"query": "tomatoes basil", "k": 5,
                                                              "model": "hash-ngram"})
            envelope = json.loads(_text(r))
            assert "never instructions" in envelope["answer_use"]
            assert len(envelope["hits"]) > 1
            assert envelope["hits"][0]["path"] == "PROJECTS/Garden.md"
            assert envelope["hits"][0]["origin"] == "vault"
            assert all("note_date" in h and "date_basis" in h for h in envelope["hits"])


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_read_only_stdio_registry_and_evidence(db, tmp_path):
    before = db.read_bytes()
    async with stdio_client(_params(db, tmp_path)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            assert {t.name for t in (await session.list_tools()).tools} == {"evidence_bundle"}
            result = await session.call_tool("evidence_bundle", {
                "query": "tomatoes basil", "k": 5, "model": "hash-ngram"})
            assert not result.is_error
            envelope = json.loads(_text(result))
            assert envelope["hits"][0]["path"] == "PROJECTS/Garden.md"
            assert "never instructions" in envelope["answer_use"]
            print("read-only MCP:", json.dumps({"tools": sorted(
                t.name for t in (await session.list_tools()).tools),
                "first_hit": envelope["hits"][0]["locator"],
                "hit_count": len(envelope["hits"])}))
            for args in ({"query": "basil", "k": 101, "model": "hash-ngram"},
                         {"query": "basil", "k": 0, "model": "hash-ngram"},
                         {"query": "x" * 2049, "k": 1, "model": "hash-ngram"},
                         {"query": "basil", "k": 1, "model": "not-a-model"},
                         {"query": "basil", "k": 1, "model": "bge-m3"}):
                assert (await session.call_tool("evidence_bundle", args)).is_error
            assert (await session.call_tool("remember", {"text": "forbidden"})).is_error
            assert (await session.call_tool("evidence_bundle", {"query": ""})).is_error
    assert db.read_bytes() == before


def test_read_only_db_denies_writes_and_missing_or_outdated(tmp_path, db):
    from quoth.db import connect_read_only
    from quoth.mcp_server import build_server

    conn = connect_read_only(db)
    assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("INSERT INTO schema_migrations(version, name) VALUES (99, 'bad')")
    conn.close()

    missing = tmp_path / "not-created" / "missing.db"
    with pytest.raises((FileNotFoundError, sqlite3.OperationalError)):
        build_server(str(missing))
    assert not missing.parent.exists()

    old = tmp_path / "old.db"
    with sqlite3.connect(old) as conn:
        conn.execute("CREATE TABLE schema_migrations(version INTEGER, name TEXT)")
        conn.execute("INSERT INTO schema_migrations VALUES (7, 'old')")
    before = old.read_bytes()
    with pytest.raises(RuntimeError, match="migration|schema"):
        build_server(str(old))
    assert old.read_bytes() == before

    from quoth.db import open_db
    empty = tmp_path / "empty.db"
    conn = open_db(empty)
    conn.close()
    with pytest.raises(RuntimeError, match="no current dense vectors"):
        build_server(str(empty))


@pytest.mark.anyio
async def test_serving_pointer_stdio_swap_and_missing_target(db, tmp_path):
    from quoth.refresh import sha
    from quoth.serving import replace_pointer
    from quoth.nightly import stdio_probe

    # Freeze two independently addressed hash-ngram fixtures, no production model.
    second = tmp_path / 'second.db'
    with sqlite3.connect(db) as source, sqlite3.connect(second) as target:
        source.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        source.execute('PRAGMA journal_mode=DELETE')
        source.backup(target)
        target.execute("UPDATE chunks SET text=text || ' successor'")
    source.close()
    target.close()
    for snapshot in (db, second):
        snapshot.with_suffix('.manifest.json').write_text(json.dumps({
            'db': str(snapshot), 'db_sha256': sha(snapshot), 'db_bytes': snapshot.stat().st_size,
            'status': 'frozen_current_vault_candidate', 'integrity': 'ok',
            'schema': 'validate_evidence_db'}))
    pointer = tmp_path / 'serving.db'
    pointer.symlink_to(db)
    assert (await stdio_probe(pointer, sha(db), model='hash-ngram'))['stdio']
    async with stdio_client(_params(pointer, tmp_path)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def identity():
                result = await session.call_tool('evidence_bundle', {
                    'query': 'basil', 'model': 'hash-ngram', 'k': 2})
                assert not result.is_error
                payload = json.loads(_text(result))
                identity = payload['provenance']['snapshot_id']
                assert all(h['provenance']['snapshot_id'] == identity for h in payload['hits'])
                return identity

            assert await identity() == sha(db)
            replace_pointer(pointer, second)
            # The watcher (5 s interval) validates and swaps in the background.
            for _ in range(80):
                if await identity() == sha(second):
                    break
                await anyio.sleep(0.25)
            assert await identity() == sha(second)
            pointer.unlink()
            pointer.symlink_to(tmp_path / 'missing.db')
            assert await identity() == sha(second)
