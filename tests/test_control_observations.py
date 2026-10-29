"""Receipt states are evidence-qualified, never inferred zeros."""
import json
from datetime import datetime, timedelta, timezone

from quoth.control.observations import Receipts


def sample(now):
    return {"schema": "quoth-receipt/v1", "kind": "freshness", "observed_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=20)).isoformat(),
            "source": {"receipt_id": "synthetic", "producer_commit": "test", "sha256": "a" * 64},
            "data": {"inventory_state": "ok", "added": 0, "removed": 0, "changed": 3,
                     "inventory_digest": "b" * 64, "measured_at": now.isoformat()}}


def test_missing_stale_corrupt_and_unknown_are_distinct(tmp_path):
    now = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
    store = Receipts(tmp_path)
    assert store.read("freshness", now)["state"] == "unavailable"  # no file is not zero
    entry = sample(now)
    path = tmp_path / "freshness.json"
    path.write_text(json.dumps(entry))
    assert store.read("freshness", now + timedelta(minutes=1))["state"] == "ok"
    assert store.read("freshness", now + timedelta(minutes=21))["state"] == "stale"
    entry["data"]["raw_path"] = "SENTINEL_PRIVATE_PATH"
    path.write_text(json.dumps(entry))
    result = store.read("freshness", now + timedelta(minutes=1))
    assert result["state"] == "invalid" and "SENTINEL" not in json.dumps(result)
    path.write_text("{broken")
    assert store.read("freshness", now)["state"] == "invalid"
    path.unlink()
    path.symlink_to(tmp_path / "elsewhere")
    assert store.read("freshness", now)["state"] == "invalid"
    assert Receipts(None).read("freshness", now)["state"] == "unavailable"
