"""Operator workflow exercises use synthetic text/vectors, never live model or vault."""
import fcntl
import json
import sqlite3

import pytest

from quoth.cli import main
from quoth.freshness import Rejected, digest, model_contract, status, successor
from quoth.refresh import code_digests, index_code_digests, inventory, sha
from test_refresh import FakeEmbedder, source


def accepted(tmp_path):
    src, vault = source(tmp_path)
    path = src.with_suffix(".manifest.json")
    data = json.loads(path.read_text())
    data.update(code_digests=code_digests(), model_manifest_sha256=model_contract(),
                source_inventory_sha256=digest(inventory(vault)))
    path.write_text(json.dumps(data))
    return src, vault


def test_fresh_byte_stable_noop_and_cli(tmp_path, capsys):
    src, vault = accepted(tmp_path)
    target = tmp_path / "unused" / "candidate.db"
    before = sha(src)
    first = status(src, vault)
    assert first["state"] == "fresh"
    assert first == status(src, vault)
    assert successor(src, target, vault)["action"] == "none"
    assert not target.parent.exists()
    assert main(["freshness", "--source", str(src), "--vault", str(vault)]) == 0
    out = capsys.readouterr().out
    assert main(["freshness", "--source", str(src), "--vault", str(vault)]) == 0
    assert capsys.readouterr().out == out
    assert "Original evidence" not in out and "a.md" not in out
    assert sha(src) == before


def test_stale_build_idempotent_handoff(tmp_path):
    src, vault = accepted(tmp_path)
    (vault / "a.md").write_text("# Private sentinel\nChanged private text.")
    assert status(src, vault)["delta"] == {"added": 0, "removed": 0, "changed": 1}
    target = tmp_path / "candidate.db"
    packet = successor(src, target, vault, embedder=FakeEmbedder())
    assert packet["action"] == "activation_required"
    assert packet["rollback"] == str(src)
    assert packet["verification"]["state"] == "fresh"
    assert "Private sentinel" not in json.dumps(packet)
    before = sha(target)
    assert successor(src, target, vault) == packet
    assert sha(target) == before
    assert target.stat().st_mode & 0o222 == 0


@pytest.mark.parametrize("field,value,reason", [
    ("code_digests", {}, "code_scope_contract"),
    ("model_manifest_sha256", "wrong", "model_contract"),
    ("vault_root", "/wrong", "vault_contract"),
])
def test_incompatible_refuses(tmp_path, field, value, reason):
    src, vault = accepted(tmp_path)
    path = src.with_suffix(".manifest.json")
    data = json.loads(path.read_text())
    data[field] = value
    path.write_text(json.dumps(data))
    expected = {"state": "incompatible", "reasons": [reason]}
    assert status(src, vault) == expected
    target = tmp_path / "candidate.db"
    assert successor(src, target, vault) == {"status": expected, "action": "none"}
    assert not target.exists()


def test_excluded_rejection_even_with_matching_manifest(tmp_path):
    src, vault = accepted(tmp_path)
    with sqlite3.connect(src) as conn:
        conn.execute("UPDATE sources SET path='ASSISTANT/private.md' WHERE path='a.md'")
    path = src.with_suffix(".manifest.json")
    data = json.loads(path.read_text())
    data.update(db_sha256=sha(src), db_bytes=src.stat().st_size)
    path.write_text(json.dumps(data))
    assert status(src, vault) == {"state": "invalid", "reasons": ["excluded_current_evidence"]}


def test_resume_interruption_and_drift(tmp_path):
    src, vault = accepted(tmp_path)
    (vault / "a.md").write_text("# Changed\nNew.")
    target = tmp_path / "candidate.db"

    def crash():
        raise RuntimeError("private sentinel")

    with pytest.raises(RuntimeError):
        successor(src, target, vault, embedder=FakeEmbedder(crash))
    assert target.with_suffix(".checkpoint.json").exists()
    calls = 0

    def drift():
        nonlocal calls
        calls += 1
        if calls == 1:
            (vault / "a.md").write_text("# Changed\nAgain.")

    packet = successor(src, target, vault, resume=True, embedder=FakeEmbedder(drift))
    assert calls == 2
    assert packet["verification"]["state"] == "fresh"


def test_drift_exhaustion_no_activation(tmp_path):
    src, vault = accepted(tmp_path)
    (vault / "a.md").write_text("# Change\nFirst.")
    target = tmp_path / "candidate.db"
    count = 0

    def drift():
        nonlocal count
        count += 1
        (vault / "a.md").write_text(f"# Change\n{count}.")

    with pytest.raises(ValueError, match="drift"):
        successor(src, target, vault, retries=1, embedder=FakeEmbedder(drift))
    assert not target.with_suffix(".manifest.json").exists()
    assert successor(src, target, vault, resume=True, embedder=FakeEmbedder())["action"] == "activation_required"


def test_busy_and_private_failure(tmp_path, capsys, monkeypatch):
    src, vault = accepted(tmp_path)
    (vault / "a.md").write_text("# Change\nFirst.")
    target = tmp_path / "candidate.db"
    with target.with_suffix(".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with pytest.raises(Rejected, match="candidate_busy"):
            successor(src, target, vault)
    def fail(*args, **kwargs):
        raise RuntimeError("private sentinel from engine")

    monkeypatch.setattr("quoth.freshness.refresh", fail)
    assert main(["successor", "--source", str(src), "--vault", str(vault),
                 "--target", str(target), "--resume"]) == 5
    assert json.loads(capsys.readouterr().out)["reasons"] == ["successor_failed_resume_or_inspect"]
    assert not target.exists()


def test_corruption_and_missing_vector_fail_closed(tmp_path):
    src, vault = accepted(tmp_path)
    with sqlite3.connect(src) as conn:
        conn.execute("DELETE FROM embeddings WHERE chunk_id=(SELECT min(chunk_id) FROM embeddings)")
    path = src.with_suffix(".manifest.json")
    data = json.loads(path.read_text())
    data.update(db_sha256=sha(src), db_bytes=src.stat().st_size)
    path.write_text(json.dumps(data))
    assert status(src, vault)["state"] == "invalid"
    path.write_text("not json private sentinel")
    assert status(src, vault) == {"state": "invalid", "reasons": ["unreadable_or_invalid_snapshot_vault"]}


def test_inventory_traversal_boundary(tmp_path, monkeypatch):
    src, vault = accepted(tmp_path)
    generated = vault / "ASSISTANT"
    generated.mkdir()
    (generated / "private.md").write_text("Private sentinel")
    (vault / "linked-directory").symlink_to(generated, target_is_directory=True)
    assert status(src, vault)["state"] == "fresh"

    def denied(*args, **kwargs):
        kwargs["onerror"](PermissionError("private directory"))
        return iter(())

    monkeypatch.setattr("quoth.refresh.os.walk", denied)
    assert status(src, vault) == {"state": "invalid", "reasons": ["unreadable_or_invalid_snapshot_vault"]}


def test_serving_change_admitted_and_successor_records_split(tmp_path):
    src, vault = accepted(tmp_path)
    path = src.with_suffix(".manifest.json")
    data = json.loads(path.read_text())
    data["code_digests"]["search.py"] = "prior-serving-version"
    path.write_text(json.dumps(data))
    assert status(src, vault)["state"] == "fresh"
    (vault / "a.md").write_text("# Changed\nNew evidence.")
    target = tmp_path / "candidate.db"
    packet = successor(src, target, vault, embedder=FakeEmbedder())
    assert packet["verification"]["state"] == "fresh"
    manifest = json.loads(target.with_suffix(".manifest.json").read_text())
    assert manifest["index_code_digests"] == index_code_digests()
    assert manifest["serving_code_digests"] == {"search.py": code_digests()["search.py"]}


@pytest.mark.parametrize("name", [k for k in code_digests() if k != "search.py"])
def test_each_legacy_index_change_refused(tmp_path, name):
    src, vault = accepted(tmp_path)
    path = src.with_suffix(".manifest.json")
    data = json.loads(path.read_text())
    data["code_digests"][name] = "changed"
    path.write_text(json.dumps(data))
    assert status(src, vault)["state"] == "incompatible"
    assert successor(src, tmp_path / "no.db", vault)["action"] == "none"


@pytest.mark.parametrize("name", list(index_code_digests()))
def test_each_split_index_change_refused(tmp_path, name):
    src, vault = accepted(tmp_path)
    path = src.with_suffix(".manifest.json")
    data = json.loads(path.read_text())
    data["index_code_digests"] = index_code_digests()
    data["index_code_digests"][name] = "changed"
    path.write_text(json.dumps(data))
    assert status(src, vault)["state"] == "incompatible"


@pytest.mark.parametrize("mutation", ["missing", "unknown"])
def test_contract_keys_fail_closed(tmp_path, mutation):
    src, vault = accepted(tmp_path)
    path = src.with_suffix(".manifest.json")
    data = json.loads(path.read_text())
    if mutation == "missing":
        del data["code_digests"]["search.py"]
    else:
        data["code_digests"]["unknown.py"] = "unknown"
    path.write_text(json.dumps(data))
    assert status(src, vault)["state"] == "incompatible"
