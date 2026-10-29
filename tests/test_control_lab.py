"""Lab-state fail-closed fixture tests, no live snapshot or server."""
import json

import pytest

from quoth.control.lab import persist


def test_atomic_aggregate_only_and_readback(tmp_path):
    record = {"schema": "quoth-lab-eval/v1", "spec_hash": "sha256:" + "a" * 64,
              "gates": {"G1_temporal_answer": False}, "ordinary_ids": ["g01"]}
    digest, path = persist(tmp_path / "lab", "evals", record)
    assert path.read_text() == json.dumps(record, ensure_ascii=False, sort_keys=True,
                                          separators=(",", ":")) + "\n"
    assert path.name == digest + ".json"
    assert persist(tmp_path / "lab", "evals", record) == (digest, path)
    assert not list(path.parent.glob("*.tmp"))
    assert not list((tmp_path / "lab").glob("candidates/*"))
    with pytest.raises(ValueError):
        persist(tmp_path / "lab", "evals", {**record, "query": "PRIVATE_SENTINEL"})
    assert b"PRIVATE_SENTINEL" not in path.read_bytes()
    with pytest.raises(ValueError):
        persist(tmp_path / "lab", "unknown", record)


def test_evaluate_fails_closed_on_another_snapshot_before_inference(conn):
    # The Q2 facet controls are bound to one snapshot digest (the shipped file is a
    # placeholder): any other snapshot is refused before a model or pipeline runs.
    from quoth.control.lab import evaluate
    from quoth.pipeline import production

    class NoModel:
        def embed_query(self, text):
            raise AssertionError("must not infer")

    spec = production("snap-1", "f" * 64)
    with pytest.raises(ValueError, match="protected_snapshot_or_gates_mismatch"):
        evaluate(conn, spec, spec, NoModel(), "snap-1", "f" * 64)
