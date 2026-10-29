"""Portable immutable full and scoped snapshot bundles — synthetic tests.

Everything here runs on synthetic SQLite data only: no live snapshots, no
vault outside tmp_path, no model downloads (embedding vectors are synthetic
float32 blobs). The source DB is built through the real ingest pipeline so
chunks/FTS/entities exercise the actual production writers.
"""

from __future__ import annotations

import json
import random
import sqlite3
import struct
from contextlib import closing
from pathlib import Path

import pytest

from quoth import bundle as bundle_mod
from quoth.bundle import (export_bundle, import_bundle,
                                validate_bundle)
from quoth.db import connect_read_only, open_db
from quoth.ingest import ingest_vault
from quoth.search import search

SENTINEL = "ZENZIBAR9-SECRET"

NOTES = {
    "SHELEST/diary.md":
        "# Diary\n\nШелест планирует поездку к морю. Kite spot checklist: board, harness, pump.\n"
        "\n## Gear\n\nТщательный подбор снаряжения.\n",
    "SHELEST/practice.md":
        "# Practice\n\nШелест практикует утреннюю медитацию и бег по выходным.\n",
    "WORK/plan.md":
        f"# Plan\n\nQuarterly delivery plan. {SENTINEL} roadmap item.\n",
    "HEALTH/bloodwork.md":
        f"# Bloodwork\n\nFerritin low. {SENTINEL} marker observed.\n",
    "KNOWLEDGE/медитация.md":
        "# Медитация\n\nЕжедневная медитация помогает сосредоточиться.\n",
}
DIARY_V2_APPEND = "\n## Sessions\n\nШелест записывает sessions длительностью 90 минут.\n"


def _write_vault(root: Path) -> Path:
    for rel, body in NOTES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return root


def _synth_vector(chunk_id: int, dim: int = 8) -> bytes:
    rng = random.Random(chunk_id)
    return struct.pack(f"<{dim}f", *[rng.uniform(-1.0, 1.0) for _ in range(dim)])


def _embed_all(conn) -> None:
    for (cid,) in conn.execute("SELECT id FROM chunks").fetchall():
        conn.execute(
            "INSERT OR IGNORE INTO embeddings(chunk_id, model, dim, vector) VALUES (?, ?, ?, ?)",
            (cid, "bge-m3", 8, _synth_vector(cid)))


def build_source_db(db_path: Path, vault: Path) -> dict:
    """Synthetic private-staging source built with the real ingest pipeline."""
    conn = open_db(db_path)
    try:
        ingest_vault(conn, vault)
        _embed_all(conn)
        # Second current version for one SHELEST note: real version history
        # (v1 retired, FTS rows for v1 dropped, both versions keep chunks).
        (vault / "SHELEST/diary.md").write_text(
            NOTES["SHELEST/diary.md"] + DIARY_V2_APPEND, encoding="utf-8")
        ingest_vault(conn, vault)
        _embed_all(conn)
        conn.execute("UPDATE sources SET scope='shelest' WHERE path LIKE 'SHELEST/%'")
        shelest_src = conn.execute(
            "SELECT id FROM sources WHERE path='SHELEST/diary.md'").fetchone()[0]
        work_src = conn.execute(
            "SELECT id FROM sources WHERE path='WORK/plan.md'").fetchone()[0]
        work_chunk = conn.execute(
            "SELECT c.id FROM chunks c JOIN source_versions v ON v.id=c.source_version_id"
            " AND v.is_current=1 JOIN sources s ON s.id=v.source_id"
            " WHERE s.path='WORK/plan.md' LIMIT 1").fetchone()[0]
        conn.execute("INSERT OR REPLACE INTO entities(chunk_id, entity, cnt)"
                     " VALUES (?, ?, 1)", (work_chunk, SENTINEL))
        return {"shelest_source": shelest_src, "work_source": work_src}
    finally:
        conn.close()


@pytest.fixture
def source_env(tmp_path: Path):
    vault = _write_vault(tmp_path / "vault")
    db = tmp_path / "staging" / "source.db"
    ids = build_source_db(db, vault)
    return db, ids


def _all_text_cells(conn) -> str:
    """Concatenate every TEXT cell of every real table (FTS included)."""
    parts = []
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    for t in tables:
        try:
            for row in conn.execute(f"SELECT * FROM {t}"):
                for v in row:
                    if isinstance(v, str):
                        parts.append(v)
        except sqlite3.Error:
            pass
    return "\n".join(parts)


def _stable_hits(conn, query: str) -> list[tuple]:
    return [(h["chunk_id"], h["path"], h["line_start"], h["line_end"], h["scope"])
            for h in search(conn, query, k=10)]


# ---------------------------------------------------------------- full bundle

def test_full_roundtrip_exact_inventory_parity(source_env, tmp_path):
    source, _ = source_env
    target = tmp_path / "out" / "full.db"
    manifest = export_bundle(source, target)
    assert manifest["kind"] == "full" and manifest["scope"] is None
    assert manifest["status"] == "complete" and manifest["errors"] == []
    assert manifest["source"]["capture"] == "sqlite_backup"
    assert set(manifest["tables"]) == set(bundle_mod.CONTENT_TABLES)
    validate_bundle(target)

    restored = tmp_path / "restored.db"
    report = import_bundle(target, restored)
    assert report["table_counts"] == manifest["source"]["table_counts"]

    with closing(connect_read_only(source)) as src, \
            closing(sqlite3.connect(restored)) as imp:
        # Exact row-level parity via canonical per-table digests.
        for table in bundle_mod.CONTENT_TABLES:
            assert bundle_mod.table_digest(src, table) == bundle_mod.table_digest(imp, table), table
        # Derived indexes rebuilt identically for current chunks.
        for fts in bundle_mod.DERIVED_TABLES:
            a = sorted(r[0] for r in src.execute(f"SELECT rowid FROM {fts}"))
            b = sorted(r[0] for r in imp.execute(f"SELECT rowid FROM {fts}"))
            assert a == b
    # Sentinels ARE part of a full bundle (nothing withheld).
    with closing(sqlite3.connect(restored)) as imp:
        assert SENTINEL in _all_text_cells(imp)


def test_query_behavior_preserved_after_import(source_env, tmp_path):
    source, _ = source_env
    target = tmp_path / "full.db"
    export_bundle(source, target)
    restored = tmp_path / "restored.db"
    import_bundle(target, restored)
    with closing(connect_read_only(source)) as src, \
            closing(sqlite3.connect(restored)) as imp:
        imp.row_factory = sqlite3.Row
        for query in ("kite", "медитация", "план roadmap", "снаряжение"):
            assert _stable_hits(src, query) == _stable_hits(imp, query), query
        # retrieval provenance survives: exact path + line ranges
        hits = _stable_hits(imp, "kite")
        assert hits and all(h[1] == "SHELEST/diary.md" for h in hits)


# --------------------------------------------------------------- scoped bundle

def test_scoped_bundle_physical_exclusion_and_closure(source_env, tmp_path):
    source, _ = source_env
    target = tmp_path / "scoped.db"
    manifest = export_bundle(source, target, scope="shelest")
    assert manifest["kind"] == "scoped" and manifest["scope"] == "shelest"
    validate_bundle(target)

    with closing(sqlite3.connect(target)) as conn:
        conn.row_factory = sqlite3.Row
        # Only shelest sources and their closure.
        paths = {r[0] for r in conn.execute("SELECT path FROM sources")}
        assert paths == {"SHELEST/diary.md", "SHELEST/practice.md"}
        # Both versions of history kept for in-scope sources.
        versions = conn.execute(
            "SELECT count(*) FROM source_versions v JOIN sources s ON s.id=v.source_id"
            " WHERE s.path='SHELEST/diary.md'").fetchone()[0]
        assert versions == 2
        assert manifest["selection"]["sources"] == {"mode": "scoped", "eligible": 5, "kept": 2}
        # Entities and vectors follow the kept chunks only.
        for table in ("entities", "embeddings"):
            assert not conn.execute(
                f"SELECT 1 FROM {table} WHERE chunk_id NOT IN (SELECT id FROM chunks)").fetchone()
        # FTS rebuilt only over current in-scope chunks.
        current = {r[0] for r in conn.execute(
            "SELECT c.id FROM chunks c JOIN source_versions v"
            " ON v.id=c.source_version_id AND v.is_current=1")}
        for fts in bundle_mod.DERIVED_TABLES:
            assert {r[0] for r in conn.execute(f"SELECT rowid FROM {fts}")} == current
        # Sentinel absent from every table cell...
        assert SENTINEL not in _all_text_cells(conn)

    # ...from the raw db bytes (physical omission, not logical filtering)...
    assert SENTINEL.encode() not in target.read_bytes()
    # ...and from the manifest sidecar.
    assert SENTINEL not in target.with_suffix(".manifest.json").read_text(encoding="utf-8")


def test_scoped_roundtrip_and_query_behavior(source_env, tmp_path):
    source, _ = source_env
    target = tmp_path / "scoped.db"
    export_bundle(source, target, scope="shelest")
    restored = tmp_path / "restored.db"
    report = import_bundle(target, restored)
    assert report["scope"] == "shelest"
    with closing(sqlite3.connect(restored)) as conn:
        conn.row_factory = sqlite3.Row
        # shelest-only results, provenance intact
        hits = _stable_hits(conn, "медитация")
        assert [h[1] for h in hits] == ["SHELEST/practice.md"]
        assert not _stable_hits(conn, "roadmap")   # WORK note excluded
        scopes = {r["scope"] for r in conn.execute("SELECT scope FROM sources")}
        assert scopes == {"shelest"}
    with closing(sqlite3.connect(target)) as b, closing(sqlite3.connect(restored)) as r:
        for table in bundle_mod.CONTENT_TABLES:
            assert bundle_mod.table_digest(b, table) == bundle_mod.table_digest(r, table)


def test_scoped_export_rejects_unknown_scope(source_env, tmp_path):
    source, _ = source_env
    with pytest.raises(ValueError, match="matches no sources"):
        export_bundle(source, tmp_path / "empty.db", scope="nobody")


# ------------------------------------------------------------ tamper/version

def _writable(p: Path) -> Path:
    p.chmod(0o644)
    return p


def test_tampered_db_rejected(source_env, tmp_path):
    source, _ = source_env
    target = tmp_path / "full.db"
    export_bundle(source, target)
    _writable(target)
    with closing(sqlite3.connect(target)) as conn:
        conn.execute("UPDATE chunks SET text = text || ' tampered'")
        conn.commit()
    with pytest.raises(ValueError, match="digest differ"):
        validate_bundle(target)


def test_tampered_manifest_rejected(source_env, tmp_path):
    source, _ = source_env
    target = tmp_path / "full.db"
    export_bundle(source, target)
    mpath = _writable(target.with_suffix(".manifest.json"))
    m = json.loads(mpath.read_text(encoding="utf-8"))
    m["tables"]["chunks"]["count"] += 1
    mpath.write_text(json.dumps(m), encoding="utf-8")
    with pytest.raises(ValueError, match="count"):
        validate_bundle(target)


def test_incompatible_format_version_rejected(source_env, tmp_path):
    source, _ = source_env
    target = tmp_path / "full.db"
    export_bundle(source, target)
    mpath = _writable(target.with_suffix(".manifest.json"))
    m = json.loads(mpath.read_text(encoding="utf-8"))
    m["format"] = "quoth-bundle/99"
    mpath.write_text(json.dumps(m), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported bundle format"):
        validate_bundle(target)
    with pytest.raises(ValueError, match="unsupported bundle format"):
        import_bundle(target, tmp_path / "x.db")


def test_incompatible_source_schema_rejected_at_export(tmp_path):
    vault = _write_vault(tmp_path / "vault")
    source = tmp_path / "odd.db"
    conn = open_db(source)
    ingest_vault(conn, vault)
    conn.execute("INSERT INTO schema_migrations(version, name) VALUES (9999, 'future')")
    conn.close()
    with pytest.raises(ValueError, match="source schema not recognized"):
        export_bundle(source, tmp_path / "out.db")


# ----------------------------------------------------- interruption/recovery

def test_interrupted_export_recovery(source_env, tmp_path, monkeypatch):
    source, _ = source_env
    target = tmp_path / "scoped.db"
    stage = target.with_name(target.name + ".stage")
    real_publish = bundle_mod._publish_without_overwrite

    def crash_at_manifest(src, dst, message):
        if Path(dst).name.endswith(".manifest.json"):
            raise KeyboardInterrupt("simulated crash between publications")
        return real_publish(src, dst, message)

    monkeypatch.setattr(bundle_mod, "_publish_without_overwrite", crash_at_manifest)
    with pytest.raises(KeyboardInterrupt):
        export_bundle(source, target, scope="shelest")
    assert not target.with_suffix(".manifest.json").exists()
    assert (stage / "checkpoint.json").is_file()
    assert target.is_file()  # db already published, still incomplete
    assert target.stat().st_mode & 0o777 == 0o444

    # Refuses to blindly restart the same target...
    with pytest.raises(ValueError, match="collision"):
        export_bundle(source, target, scope="shelest")
    # ...and refuses to resume against a drifted source.
    drift = open_db(source)
    drift.execute("INSERT INTO sources(path, scope) VALUES ('SHELEST/late.md', 'shelest')")
    drift.close()
    with pytest.raises(ValueError, match="does not match"):
        export_bundle(source, target, scope="shelest", resume=True)

    # Re-running export from a fresh target still works after the drift.
    fresh = tmp_path / "scoped2.db"
    monkeypatch.setattr(bundle_mod, "_publish_without_overwrite", real_publish)
    manifest = export_bundle(source, fresh, scope="shelest")
    validate_bundle(fresh)
    assert manifest["tables"]["sources"]["count"] == 3
    assert not fresh.with_name(fresh.name + ".stage").exists()


def test_midbuild_crash_and_resume(source_env, tmp_path, monkeypatch):
    source, _ = source_env
    target = tmp_path / "full.db"
    stage = target.with_name(target.name + ".stage")

    def boom(conn):
        raise RuntimeError("simulated crash during build")

    monkeypatch.setattr(bundle_mod, "_rebuild_fts", boom)
    with pytest.raises(RuntimeError, match="crash during build"):
        export_bundle(source, target)
    assert (stage / "checkpoint.json").is_file()
    assert (stage / "snapshot.db").is_file()
    assert (stage.stat().st_mode & 0o777) == 0o700
    assert not target.exists()

    monkeypatch.undo()
    manifest = export_bundle(source, target, resume=True)
    assert manifest["kind"] == "full"
    validate_bundle(target)
    assert not stage.exists()


# ------------------------------------------------------ consistent capture

def test_no_raw_copy_of_active_writer(tmp_path):
    """Committed-but-WAL-resident rows must arrive via backup, not file bytes."""
    vault = _write_vault(tmp_path / "vault")
    source = tmp_path / "live.db"
    writer = open_db(source)
    ingest_vault(writer, vault)
    _embed_all(writer)
    wal_note = "# Walwalker\n\nZENZIBAR9-SECRET wal-resident discovery note.\n"
    (vault / "WORK/walwalker.md").write_text(wal_note, encoding="utf-8")
    ingest_vault(writer, vault)  # committed into -wal, main file not checkpointed
    _embed_all(writer)

    main_before = bundle_mod.sha(source)
    assert (tmp_path / "live.db-wal").exists(), "fixture sanity: WAL must be live"
    assert wal_note.encode() not in Path(source).read_bytes(), \
        "fixture sanity: the new note must not yet be in the main db file"

    target = tmp_path / "bundle.db"
    export_bundle(source, target)
    validate_bundle(target)

    # Source untouched (no checkpoint forced, no bytes changed), WAL intact.
    assert bundle_mod.sha(source) == main_before
    assert (tmp_path / "live.db-wal").exists()
    with closing(sqlite3.connect(target)) as conn:
        got = conn.execute(
            "SELECT count(*) FROM sources WHERE path='WORK/walwalker.md'").fetchone()[0]
        assert got == 1
        assert "wal-resident discovery note" in conn.execute(
            "SELECT text FROM chunks ORDER BY id DESC LIMIT 1").fetchone()[0]
    # Data committed after the export is not in the frozen bundle.
    writer.execute("INSERT INTO sources(path, scope) VALUES ('WORK/late.md', 'work')")
    with closing(sqlite3.connect(target)) as conn:
        assert not conn.execute("SELECT 1 FROM sources WHERE path='WORK/late.md'").fetchone()
    writer.close()


def test_import_target_collision_rejected(source_env, tmp_path):
    source, _ = source_env
    target = tmp_path / "full.db"
    export_bundle(source, target)
    restored = tmp_path / "restored.db"
    import_bundle(target, restored)
    with pytest.raises(ValueError, match="already exists"):
        import_bundle(target, restored)
    # Frozen bundle cannot be re-exported over either.
    with pytest.raises(ValueError, match="already exists"):
        export_bundle(source, target)


# ------------------------------------------------------------------ CLI path

def test_cli_roundtrip(source_env, tmp_path, capsys, monkeypatch):
    from quoth.cli import main
    source, _ = source_env
    target = tmp_path / "cli.db"
    assert main(["bundle", "export", "--source", str(source), "--target", str(target),
                 "--scope", "shelest"]) == 0
    assert main(["bundle", "validate", "--db", str(target)]) == 0
    assert main(["bundle", "import", "--db", str(target),
                 "--target", str(tmp_path / "cli-restored.db")]) == 0
    out = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert out["scope"] == "shelest" and out["compatibility_drift"] == []
    monkeypatch.setattr("sys.argv", ["quoth"])
