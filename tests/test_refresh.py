"""Refresh lifecycle with synthetic vault and deterministic vectors (no ONNX)."""
import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from quoth.db import connect_read_only, open_db
from quoth.embed.store import embed_chunks
from quoth.ingest import ingest_vault
from quoth.refresh import (
    code_digests,
    db_inventory,
    index_code_digests,
    inventory,
    refresh,
    sha,
    validate,
    validate_code_compatibility,
)


class FakeEmbedder:
    spec = SimpleNamespace(name="bge-m3", dim=1024)

    def __init__(self, on_embed=None):
        self.on_embed = on_embed

    def embed_passages(self, texts):
        if self.on_embed:
            self.on_embed()
        out = np.zeros((len(texts), 1024), dtype="<f4")
        out[:, 0] = 1
        return out


def source(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("# Alpha\nOriginal evidence.\n")
    (vault / "deleted.md").write_text("# Deleted\nOld text.\n")
    src = tmp_path / "src.db"
    conn = open_db(src)
    ingest_vault(conn, vault)
    embed_chunks(conn, FakeEmbedder())
    counts = validate(conn, inventory(vault))
    inventory_digest = hashlib.sha256(json.dumps(
        db_inventory(conn), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()
    manifest = {"db": str(src), "db_sha256": sha(src), "db_bytes": src.stat().st_size,
                "status": "frozen_current_vault_candidate", "code_digests": code_digests(),
                "index_code_digests": index_code_digests(),
                "vault_root": str(vault), "model": "BAAI/bge-m3", "integrity": "ok",
                "schema": "validate_evidence_db", "counts": counts,
                "source_inventory_sha256": inventory_digest}
    src.with_suffix(".manifest.json").write_text(json.dumps(manifest))
    return src, vault


def test_changed_new_deleted_and_reuse(tmp_path):
    src, vault = source(tmp_path)
    (vault / "a.md").write_text("# Alpha\nChanged text.\n")
    (vault / "new.md").write_text("# New\nAnother item.\n")
    (vault / "deleted.md").unlink()
    target = tmp_path / "target.db"
    original = sha(src)
    result = refresh(src, target, vault, embedder=FakeEmbedder())
    assert (result["ingest"]["changed"], result["ingest"]["new"],
            result["ingest"]["removed"]) == (1, 1, 1)
    assert result["embed"]["embedded"] == 2
    with connect_read_only(target) as db:
        assert db_inventory(db) == inventory(vault)
        assert validate(db, inventory(vault)) == result["counts"]
    assert sha(src) == original
    assert sha(target) == result["db_sha256"]
    assert not target.with_suffix(".checkpoint.json").exists()
    with pytest.raises(ValueError, match="frozen output"):
        refresh(src, target, vault, embedder=FakeEmbedder())
    again = refresh(src, tmp_path / "again.db", vault, embedder=FakeEmbedder())
    assert again["counts"] == result["counts"]


def test_unchanged_source_embeds_nothing(tmp_path):
    src, vault = source(tmp_path)
    protected = vault / ".trash" / "unindexed.md"
    protected.parent.mkdir()
    protected.write_text("# Protected\nExcluded.\n")
    (vault / ".cursor").mkdir()
    (vault / ".cursor" / "rules.md").write_text("# Excluded\n")
    generated = vault / "ASSISTANT" / "dreams" / "generated.md"
    generated.parent.mkdir(parents=True)
    generated.write_text("# Generated\nNot memory evidence.\n")
    result = refresh(src, tmp_path / "unchanged.db", vault, embedder=FakeEmbedder())
    assert result["embed"]["embedded"] == 0
    assert result["ingest"]["unchanged"] == 2
    assert result["counts"]["notes_current"] == 2
    assert "ASSISTANT/dreams/generated.md" not in inventory(vault)


def test_drift_retry_and_resumable_failure(tmp_path):
    src, vault = source(tmp_path)
    (vault / "new.md").write_text("# New\nFirst.\n")
    calls = 0

    def mutate_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            (vault / "new.md").write_text("# New\nSecond.\n")

    target = tmp_path / "drift.db"
    result = refresh(src, target, vault, retries=2, embedder=FakeEmbedder(mutate_once))
    assert calls == 2
    assert result["counts"]["notes_current"] == 3
    with connect_read_only(target) as db:
        assert db_inventory(db) == inventory(vault)

    (vault / "later.md").write_text("# Later\nInitial.\n")
    fail = tmp_path / "failure.db"

    def crash():
        raise RuntimeError("synthetic embedding interruption")

    with pytest.raises(RuntimeError, match="interruption"):
        refresh(src, fail, vault, embedder=FakeEmbedder(crash))
    assert fail.exists() and fail.with_suffix(".checkpoint.json").exists()
    with pytest.raises(ValueError, match="collision"):
        refresh(src, fail, vault, embedder=FakeEmbedder())
    resumed = refresh(src, fail, vault, resume=True, embedder=FakeEmbedder())
    assert resumed["counts"]["notes_current"] == 4


def test_persistent_drift_exhaustion(tmp_path):
    src, vault = source(tmp_path)
    (vault / "new.md").write_text("# New\nFirst.\n")
    count = 0

    def mutate():
        nonlocal count
        count += 1
        (vault / "new.md").write_text(f"# New\nRevision {count}.\n")

    target = tmp_path / "unstable.db"
    with pytest.raises(ValueError, match="retry budget exhausted"):
        refresh(src, target, vault, retries=1, embedder=FakeEmbedder(mutate))
    assert target.with_suffix(".checkpoint.json").is_file()
    assert not target.with_suffix(".manifest.json").exists()
    refresh(src, target, vault, resume=True, embedder=FakeEmbedder())


def test_invalid_source_and_target_collision(tmp_path):
    src, vault = source(tmp_path)
    target = tmp_path / "target.db"
    target.touch()
    with pytest.raises(ValueError, match="collision"):
        refresh(src, target, vault, embedder=FakeEmbedder())
    assert target.stat().st_size == 0
    src.with_suffix(".manifest.json").write_text('{"db_sha256": "wrong"}')
    with pytest.raises(ValueError, match="incompatible"):
        refresh(src, tmp_path / "other.db", vault, embedder=FakeEmbedder())
    assert not (tmp_path / "other.db").exists()


def test_scope_contract_migration_is_explicit_and_narrow():
    current = code_digests()
    source = dict(current)
    source["scopes.toml"] = "old-scope-contract"
    manifest = {"code_digests": source}
    with pytest.raises(ValueError, match="code compatibility"):
        validate_code_compatibility(manifest, current)
    validate_code_compatibility(manifest, current, allow_scope_contract_change=True)

    source["chunker.py"] = "unrelated-change"
    with pytest.raises(ValueError, match="code compatibility"):
        validate_code_compatibility(
            {"code_digests": source}, current, allow_scope_contract_change=True
        )


def test_snapshot_builds_a_first_frozen_snapshot_that_refresh_accepts(tmp_path):
    from quoth.refresh import snapshot
    from quoth.serving import ServingSnapshot
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("# Alpha\nFirst evidence.\n")
    first = tmp_path / "snap" / "s1.db"
    manifest = snapshot(first, vault, embedder=FakeEmbedder())
    assert manifest["status"] == "frozen_current_vault_candidate"
    assert manifest["source_db_sha256"] is None and manifest["counts"]["notes_current"] == 1
    assert not first.with_suffix(".checkpoint.json").exists()
    assert ServingSnapshot(str(first)).conn.execute("SELECT count(*) FROM chunks").fetchone()[0] > 0
    with pytest.raises(ValueError, match="collision"):
        snapshot(first, vault, embedder=FakeEmbedder())
    (vault / "b.md").write_text("# Beta\nSecond evidence.\n")
    second = refresh(first, tmp_path / "snap" / "s2.db", vault, embedder=FakeEmbedder())
    assert second["source_db_sha256"] == sha(first) and second["counts"]["notes_current"] == 2
