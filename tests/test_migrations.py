import sqlite3

import pytest

from quoth.db import connect, list_migrations, migrate

TABLES = {
    "sources", "source_versions", "chunks", "chunks_fts", "chunks_fts_trigram",
    "embeddings", "entities", "schema_migrations",
}


def _cols(c, t):
    return [r[1] for r in c.execute(f"PRAGMA table_info({t})")]


def test_migrations_numbered_and_idempotent(db_path):
    c = connect(db_path)
    first = migrate(c)
    assert first == [n for _, n, _ in list_migrations()]
    assert migrate(c) == []
    c.close()
    c2 = connect(db_path)
    assert migrate(c2) == []
    assert c2.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == len(first)


def test_schema_is_one_clean_initial_migration():
    assert [(v, n) for v, n, _ in list_migrations()] == [(1, "0001_evidence.sql")]


def test_schema_tables_and_columns(conn):
    shadow = ("_data", "_idx", "_docsize", "_config", "_content")
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
             if not r[0].endswith(shadow)}
    assert names == TABLES
    assert _cols(conn, "sources") == ["id", "path", "scope"]
    assert _cols(conn, "source_versions") == ["id", "source_id", "content_hash", "ingested_at",
                                              "is_current", "metadata"]
    assert _cols(conn, "chunks") == ["id", "source_version_id", "heading_path", "line_start",
                                     "line_end", "text"]
    assert _cols(conn, "embeddings") == ["chunk_id", "model", "dim", "vector"]
    assert _cols(conn, "entities") == ["chunk_id", "entity", "cnt"]
    fts = {r[0]: r[1] for r in conn.execute("SELECT name, sql FROM sqlite_master WHERE name LIKE 'chunks_fts%' AND type='table' AND sql LIKE 'CREATE VIRTUAL%'")}
    assert "unicode61 remove_diacritics 2" in fts["chunks_fts"]
    assert "trigram" in fts["chunks_fts_trigram"]


def test_constraints_and_append_only(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO sources(path, scope) VALUES ('x.md', '')")
    sid = conn.execute("INSERT INTO sources(path, scope) VALUES ('x.md', 'general')").lastrowid
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO sources(path, scope) VALUES ('x.md', 'work')")
    vid = conn.execute("INSERT INTO source_versions(source_id, content_hash) VALUES (?, 'h')",
                       (sid,)).lastrowid
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO source_versions(source_id, content_hash) VALUES (?, 'h2')", (sid,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO chunks(source_version_id, line_start, line_end, text)"
                     " VALUES (?, 3, 2, 't')", (vid,))
    cid = conn.execute("INSERT INTO chunks(source_version_id, line_start, line_end, text)"
                       " VALUES (?, 1, 2, 't')", (vid,)).lastrowid
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO embeddings(chunk_id, model, dim, vector) VALUES (?, 'm', 2, ?)",
                     (cid, b"\0" * 4))
    conn.execute("INSERT INTO embeddings(chunk_id, model, dim, vector) VALUES (?, 'm', 2, ?)",
                 (cid, b"\0" * 8))
    for table in ("chunks", "source_versions", "sources"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute(f"DELETE FROM {table}")
