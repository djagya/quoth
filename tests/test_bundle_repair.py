"""Bundle export repair — independent regression tests (synthetic data only).

These reproduce three export defects found in review and pin the corrected contract:

1. A pre-existing ``<target>.work`` sentinel must never be mutated by an
   export (old code unlinked/replaced/deleted it).
2. A crash *during* manifest serialization must not leave a partial
   manifest at the published name, and ``resume=True`` must recover.
3. A failed scoped export under umask 022 must not leave world-readable
   staging containing the unrelated full corpus.

Plus the collision/ownership contract around them: foreign artifacts at any
sidecar/staging name are refused, never deleted; racing exports/imports
collide safely; private modes hold regardless of umask.

Everything here runs on synthetic SQLite data only: no live snapshots, no
vault outside tmp_path, no providers, no network.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from quoth import bundle as bundle_mod
from quoth.bundle import export_bundle, import_bundle, validate_bundle

from test_bundle import SENTINEL, _write_vault, build_source_db


def stage_dir(target: Path) -> Path:
    return target.with_name(target.name + ".stage")


@pytest.fixture
def source_env(tmp_path: Path) -> Path:
    vault = _write_vault(tmp_path / "vault")
    db = tmp_path / "staging" / "source.db"
    build_source_db(db, vault)
    return db


class _Umask:
    """umask context manager (process-global; tests run single-threaded)."""

    def __init__(self, mask: int):
        self.mask = mask
        self.saved = None

    def __enter__(self):
        self.saved = os.umask(self.mask)
        return self

    def __exit__(self, *exc):
        assert self.saved is not None
        os.umask(self.saved)


def _walk(out: Path):
    return sorted(p for p in out.rglob("*") if p.name != out.name)


# ------------------------------------------------- defect 1: foreign artifacts

def test_preexisting_work_sentinel_preserved(source_env: Path, tmp_path: Path):
    """A foreign <target>.work file must survive a rejected-or-accepted export.

    Old code let capture_snapshot unlink/replace it and deleted it on success.
    """
    source = source_env
    target = tmp_path / "out" / "collision.db"
    work = target.with_name(target.name + ".work")
    work.parent.mkdir(parents=True, exist_ok=True)
    work.write_bytes(b"OWNED-BY-ANOTHER-OPERATION")

    export_bundle(source, target, scope="shelest")
    validate_bundle(target)
    assert work.read_bytes() == b"OWNED-BY-ANOTHER-OPERATION"


def test_preexisting_legacy_sidecars_preserved(source_env: Path, tmp_path: Path):
    """Old sidecar names (.checkpoint.json/.copying) are never written now;
    whatever sits there is left byte-identical either way."""
    source = source_env
    target = tmp_path / "out" / "sidecars.db"
    for name in (target.name + ".checkpoint.json", target.name + ".copying",
                 target.name + ".work"):
        sentinel = target.with_name(name)
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_bytes(f"FOREIGN:{name}".encode())
    export_bundle(source, target, scope="shelest")
    validate_bundle(target)
    for name in (target.name + ".checkpoint.json", target.name + ".copying",
                 target.name + ".work"):
        assert target.with_name(name).read_bytes() == f"FOREIGN:{name}".encode()


def test_published_bundle_is_never_replaced(source_env: Path, tmp_path: Path):
    source = source_env
    target = tmp_path / "out" / "frozen.db"
    export_bundle(source, target, scope="shelest")
    db_bytes = target.read_bytes()
    m_bytes = target.with_suffix(".manifest.json").read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        export_bundle(source, target, scope="shelest")
    assert target.read_bytes() == db_bytes
    assert target.with_suffix(".manifest.json").read_bytes() == m_bytes


# ------------------------------------------- defect 2: mid-serialization crash

def test_crash_during_manifest_serialization_then_resume(
        source_env: Path, tmp_path: Path, monkeypatch):
    """Crash *inside* json.dump of the manifest: nothing partial may appear
    at the published name, and resume must complete the bundle."""
    source = source_env
    target = tmp_path / "out" / "crash.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    manifest_path = target.with_suffix(".manifest.json")
    real_dump = bundle_mod.json.dump

    def crash(obj, file, *args, **kwargs):
        if isinstance(obj, dict) and obj.get("status") == "complete":
            file.write('{"format":')
            file.flush()
            raise RuntimeError("simulated mid-manifest write crash")
        return real_dump(obj, file, *args, **kwargs)

    monkeypatch.setattr(bundle_mod.json, "dump", crash)
    with pytest.raises(RuntimeError, match="mid-manifest"):
        export_bundle(source, target, scope="shelest")
    monkeypatch.setattr(bundle_mod.json, "dump", real_dump)

    # No partial manifest was published: the final name either does not
    # exist or holds a complete document.
    assert not os.path.lexists(manifest_path)

    report = export_bundle(source, target, scope="shelest", resume=True)
    assert report["status"] == "complete"
    validate_bundle(target)
    assert not stage_dir(target).exists()


def test_crash_between_db_and_manifest_publish_adopts_own_target(
        source_env: Path, tmp_path: Path, monkeypatch):
    """Bounded recovery: resume may adopt the db this staging published, but
    never a foreign file that appeared at the target name."""
    source = source_env
    target = tmp_path / "out" / "window.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    real_publish = bundle_mod._publish_without_overwrite

    def crash_at_manifest(src, dst, message):
        if str(dst).endswith(".manifest.json"):
            raise KeyboardInterrupt("simulated crash between publishes")
        return real_publish(src, dst, message)

    monkeypatch.setattr(bundle_mod, "_publish_without_overwrite", crash_at_manifest)
    with pytest.raises(KeyboardInterrupt):
        export_bundle(source, target, scope="shelest")
    assert target.is_file()
    assert not os.path.lexists(target.with_suffix(".manifest.json"))
    published_sha = bundle_mod.sha(target)

    monkeypatch.setattr(bundle_mod, "_publish_without_overwrite", real_publish)
    report = export_bundle(source, target, scope="shelest", resume=True)
    validate_bundle(target)
    assert bundle_mod.sha(target) == published_sha
    assert report["db"]["sha256"] == published_sha
    assert not stage_dir(target).exists()


# ---------------------------------------------- defect 3: staging privacy

def test_failed_scoped_export_privacy_under_umask_022(
        source_env: Path, tmp_path: Path, monkeypatch):
    """A failed scoped export must leave only private artifacts (dirs 0700,
    files 0600) regardless of umask, and the unrelated full corpus may exist
    only inside that private staging."""
    source = source_env
    out = tmp_path / "out"
    target = out / "scope-failure.db"
    source_sha = bundle_mod.sha(source)

    def boom(*args, **kwargs):
        raise RuntimeError("mid-build")

    monkeypatch.setattr(bundle_mod, "_build_bundle", boom)
    with _Umask(0o022):
        with pytest.raises(RuntimeError, match="mid-build"):
            export_bundle(source, target, scope="shelest")

    # Source untouched.
    assert bundle_mod.sha(source) == source_sha

    stage = stage_dir(target)
    assert stage.is_dir(), "interrupted export remains resumable via staging"
    assert stage.stat().st_mode & 0o777 == 0o700

    # Every artifact left under the output dir is private.
    for p in _walk(out):
        mode = p.lstat().st_mode & 0o777
        assert mode & 0o077 == 0, f"non-private artifact {p}: {oct(mode)}"

    # The full-corpus capture (with the unrelated secret) exists only inside
    # the private staging snapshot.
    snap = stage / "snapshot.db"
    assert snap.is_file()
    assert snap.stat().st_mode & 0o777 == 0o600
    assert SENTINEL.encode() in snap.read_bytes()
    for p in _walk(out):
        if p.is_file() and p != snap:
            assert SENTINEL.encode() not in p.read_bytes(), p

    # And the failed export stays resumable: a clean run completes privately.
    monkeypatch.undo()
    with _Umask(0o022):
        report = export_bundle(source, target, scope="shelest", resume=True)
    assert report["status"] == "complete"
    validate_bundle(target)
    assert not stage_dir(target).exists()
    assert target.stat().st_mode & 0o777 == 0o444


# ------------------------------------------------- collisions and ownership

def test_two_colliding_exports_and_live_owner_refusal(
        source_env: Path, tmp_path: Path, monkeypatch):
    """A second exporter must be refused without mutating the first one's
    staging; a live foreign owner blocks resume; the same thread may retry."""
    source = source_env
    target = tmp_path / "out" / "race.db"
    target.parent.mkdir(parents=True, exist_ok=True)

    def boom(conn):
        raise RuntimeError("crash during build")

    monkeypatch.setattr(bundle_mod, "_rebuild_fts", boom)
    with pytest.raises(RuntimeError, match="crash during build"):
        export_bundle(source, target, scope="shelest")          # attempt #1
    stage = stage_dir(target)
    before = {p.name: p.read_bytes() for p in stage.iterdir()}

    with pytest.raises(ValueError, match="collision"):
        export_bundle(source, target, scope="shelest")          # attempt #2
    assert {p.name: p.read_bytes() for p in stage.iterdir()} == before

    # Different parameters never take over someone's staging.
    with pytest.raises(ValueError):
        export_bundle(source, target, scope="other", resume=True)

    # A concurrent thread's live owner is refused...
    owner_path = stage / "owner.json"
    owner = json.loads(owner_path.read_text(encoding="utf-8"))
    owner["thread"] = threading.get_ident() ^ 0x5A5A
    owner_path.write_text(json.dumps(owner), encoding="utf-8")
    with pytest.raises(ValueError, match="live"):
        export_bundle(source, target, scope="shelest", resume=True)

    # ...while the owning thread itself (crashed attempt unwound) recovers.
    owner["thread"] = threading.get_ident()
    owner_path.write_text(json.dumps(owner), encoding="utf-8")
    monkeypatch.undo()
    export_bundle(source, target, scope="shelest", resume=True)
    validate_bundle(target)
    assert not stage.exists()


def test_dead_owner_staging_is_resumable(source_env: Path, tmp_path: Path,
                                         monkeypatch):
    source = source_env
    target = tmp_path / "out" / "dead.db"
    target.parent.mkdir(parents=True, exist_ok=True)

    def boom(conn):
        raise RuntimeError("crash during build")

    monkeypatch.setattr(bundle_mod, "_rebuild_fts", boom)
    with pytest.raises(RuntimeError):
        export_bundle(source, target, scope="shelest")
    monkeypatch.undo()

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    owner_path = stage_dir(target) / "owner.json"
    owner = json.loads(owner_path.read_text(encoding="utf-8"))
    owner["pid"] = dead.pid
    owner_path.write_text(json.dumps(owner), encoding="utf-8")

    report = export_bundle(source, target, scope="shelest", resume=True)
    assert report["status"] == "complete"
    validate_bundle(target)
    assert not stage_dir(target).exists()


def test_foreign_stage_artifacts_refused_not_deleted(source_env: Path,
                                                     tmp_path: Path):
    source = source_env
    target = tmp_path / "out" / "f.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = stage_dir(target)

    # Foreign file where staging would go.
    stage.write_bytes(b"FOREIGN-STAGE-FILE")
    with pytest.raises(ValueError):
        export_bundle(source, target, scope="shelest")
    assert stage.read_bytes() == b"FOREIGN-STAGE-FILE"
    stage.unlink()

    # Foreign directory with unknown content.
    stage.mkdir()
    (stage / "not-ours.txt").write_bytes(b"x")
    with pytest.raises(ValueError):
        export_bundle(source, target, scope="shelest")
    assert (stage / "not-ours.txt").read_bytes() == b"x"
    with pytest.raises(ValueError):
        export_bundle(source, target, scope="shelest", resume=True)
    assert (stage / "not-ours.txt").read_bytes() == b"x"
    shutil.rmtree(stage)

    # Foreign symlink where staging would go.
    foreign = tmp_path / "foreign-target"
    foreign.write_bytes(b"F")
    stage.symlink_to(foreign)
    with pytest.raises(ValueError):
        export_bundle(source, target, scope="shelest")
    assert stage.is_symlink() and foreign.read_bytes() == b"F"
    stage.unlink()

    # Dangling symlink at the manifest path is frozen output, not a hole.
    target2 = tmp_path / "out" / "g.db"
    target2.with_suffix(".manifest.json").symlink_to(tmp_path / "nowhere.json")
    with pytest.raises(ValueError, match="already exists"):
        export_bundle(source, target2, scope="shelest")

    # Nothing was published by any refused attempt above.
    assert not target.exists() and not target2.exists()


def test_resume_refuses_symlink_inside_staging(source_env: Path, tmp_path: Path,
                                               monkeypatch):
    source = source_env
    target = tmp_path / "out" / "sym.db"
    target.parent.mkdir(parents=True, exist_ok=True)

    def boom(conn):
        raise RuntimeError("crash during build")

    monkeypatch.setattr(bundle_mod, "_rebuild_fts", boom)
    with pytest.raises(RuntimeError):
        export_bundle(source, target, scope="shelest")
    monkeypatch.undo()

    snap = stage_dir(target) / "snapshot.db"
    snap.unlink()
    snap.symlink_to(source)
    with pytest.raises(ValueError, match="symlink"):
        export_bundle(source, target, scope="shelest", resume=True)
    assert snap.is_symlink() and source.is_file()


def test_resume_refuses_malformed_markers_without_deleting(
        source_env: Path, tmp_path: Path, monkeypatch):
    source = source_env
    target = tmp_path / "out" / "mal.db"
    target.parent.mkdir(parents=True, exist_ok=True)

    def boom(conn):
        raise RuntimeError("crash during build")

    monkeypatch.setattr(bundle_mod, "_rebuild_fts", boom)
    with pytest.raises(RuntimeError):
        export_bundle(source, target, scope="shelest")
    monkeypatch.undo()

    stage = stage_dir(target)
    (stage / "owner.json").write_text('{"partial":', encoding="utf-8")
    with pytest.raises(ValueError, match="malformed"):
        export_bundle(source, target, scope="shelest", resume=True)
    assert (stage / "owner.json").read_text(encoding="utf-8") == '{"partial":'

    # Restore a valid owner, corrupt the checkpoint: still refused intact.
    owner = {"stage_format": "quoth-bundle-stage/1", "target": str(target),
             "source": str(Path(source).resolve()), "scope": "shelest",
             "pid": os.getpid(), "thread": threading.get_ident(),
             "created_at": "2026-10-14T00:00:00Z"}
    (stage / "owner.json").write_text(json.dumps(owner), encoding="utf-8")
    (stage / "checkpoint.json").write_bytes(b"\x00\x01not-json")
    with pytest.raises(ValueError, match="malformed"):
        export_bundle(source, target, scope="shelest", resume=True)
    assert (stage / "checkpoint.json").read_bytes() == b"\x00\x01not-json"


# ------------------------------------------------------------------ imports

def test_import_privacy_and_collision(source_env: Path, tmp_path: Path):
    source = source_env
    bundle = tmp_path / "out" / "b.db"
    export_bundle(source, bundle, scope="shelest")

    restored = tmp_path / "restored.db"
    with _Umask(0o022):
        import_bundle(bundle, restored)
    assert restored.stat().st_mode & 0o777 == 0o600

    # Colliding second import refused; existing target preserved.
    with pytest.raises(ValueError, match="already exists"):
        import_bundle(bundle, restored)

    # A pre-existing foreign target is never touched.
    foreign = tmp_path / "foreign.db"
    foreign.write_bytes(b"NOT-MINE")
    with pytest.raises(ValueError, match="already exists"):
        import_bundle(bundle, foreign)
    assert foreign.read_bytes() == b"NOT-MINE"


def test_failed_import_removes_own_partial_target(source_env: Path,
                                                  tmp_path: Path, monkeypatch):
    source = source_env
    bundle = tmp_path / "out" / "b.db"
    export_bundle(source, bundle, scope="shelest")

    calls = {"n": 0}
    real = bundle_mod._verify_against_manifest

    def flaky(conn, m):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("post-restore verification crash")
        return real(conn, m)

    monkeypatch.setattr(bundle_mod, "_verify_against_manifest", flaky)
    target = tmp_path / "partial.db"
    with pytest.raises(RuntimeError, match="post-restore"):
        import_bundle(bundle, target)
    assert not os.path.lexists(target)

    # Retry after the transient fault succeeds into the same name.
    monkeypatch.undo()
    report = import_bundle(bundle, target)
    assert report["scope"] == "shelest"
