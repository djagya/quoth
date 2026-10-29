"""Allowlisted, age-qualified controller receipts. GET never refreshes or probes.

Projected receipts have no free-form path/query/text/title keys. Invalid, absent,
expired or incompatible data is never converted into a green zero.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

NAMES = ("freshness", "runtime", "quality")
FIELDS = {
    "freshness": {"inventory_state", "added", "removed", "changed", "inventory_digest", "measured_at"},
    "runtime": {"profile", "effective_tools", "effective_snapshot", "observed_at", "observer_state"},
    "quality": {"suite_id", "suite_digest", "snapshot_sha256", "metrics", "denominators", "gate_verdicts", "serving"},
}
MAX_BYTES = 1024 * 1024


def iso(value):
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError
        return dt.astimezone(timezone.utc)
    except (ValueError, AttributeError, TypeError):
        return None


def unavailable(name: str, state="unavailable", reason="receipt_missing"):
    return {"schema": "quoth-control/v1", "state": state, "observed_at": None,
            "expires_at": None, "source": None, "data": None, "reasons": [reason], "kind": name}


class Receipts:
    def __init__(self, directory: Path | None):
        self.directory = Path(directory) if directory is not None else None

    def read(self, name: str, now: datetime | None = None):
        if name not in NAMES:
            raise ValueError("unknown receipt kind")
        if self.directory is None:
            return unavailable(name)
        path = self.directory / (name + ".json")
        try:
            if not path.exists() and not path.is_symlink():
                return unavailable(name)
            if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
                return unavailable(name, "invalid", "receipt_invalid")
            raw = path.read_bytes()
            if len(raw) > MAX_BYTES:
                return unavailable(name, "invalid", "receipt_invalid")
            entry = json.loads(raw)
            if not isinstance(entry, dict) or set(entry) != {"schema", "kind", "observed_at", "expires_at", "source", "data"}:
                return unavailable(name, "invalid", "receipt_shape")
            if entry["schema"] != "quoth-receipt/v1" or entry["kind"] != name:
                return unavailable(name, "incompatible", "receipt_version")
            source, data = entry["source"], entry["data"]
            if not isinstance(source, dict) or set(source) != {"receipt_id", "producer_commit", "sha256"} or not all(
                isinstance(x, str) and 1 <= len(x) <= 128 for x in source.values()
            ):
                return unavailable(name, "invalid", "receipt_provenance")
            if not isinstance(data, dict) or set(data) != FIELDS[name]:
                return unavailable(name, "invalid", "receipt_fields")
            # Strictly JSON primitive aggregate fields; no nested free-text or paths.
            if not safe_aggregate(name, data):
                return unavailable(name, "invalid", "receipt_content")
            observed, expiry = iso(entry["observed_at"]), iso(entry["expires_at"])
            if not observed or not expiry or not observed < expiry or expiry - observed > timedelta(minutes=20):
                return unavailable(name, "invalid", "receipt_time")
            now = now or datetime.now(timezone.utc)
            if observed > now:
                return unavailable(name, "invalid", "receipt_future")
            state = "stale" if now >= expiry else "ok"
            return {"schema": "quoth-control/v1", "state": state, "observed_at": entry["observed_at"],
                    "expires_at": entry["expires_at"], "source": source, "data": data,
                    "reasons": ["receipt_expired"] if state == "stale" else [], "kind": name}
        except (OSError, ValueError, UnicodeError, TypeError, KeyError):
            return unavailable(name, "invalid", "receipt_unreadable")


def safe_aggregate(name, data):
    """Reject free-text, locators and per-item content even in a controller receipt."""
    if name == "freshness":
        return data["inventory_state"] in {"ok", "stale", "unavailable", "invalid"} and all(
            type(data[key]) is int and 0 <= data[key] <= 1000000 for key in ("added", "removed", "changed")
        ) and isinstance(data["inventory_digest"], str) and len(data["inventory_digest"]) == 64 and iso(data["measured_at"]) is not None
    if name == "runtime":
        return data["profile"] in {"default", "unknown"} and data["observer_state"] in {"observed", "unavailable"} and isinstance(data["effective_tools"], list) and set(data["effective_tools"]) <= {"evidence_bundle"} and (data["effective_snapshot"] is None or isinstance(data["effective_snapshot"], str) and len(data["effective_snapshot"]) <= 128) and iso(data["observed_at"]) is not None
    if name == "quality":
        return all(isinstance(data[key], str) and len(data[key]) <= 128 for key in ("suite_id", "suite_digest", "snapshot_sha256")) and type(data["serving"]) is bool and numeric_map(data["metrics"], 32) and numeric_map(data["denominators"], 32, integer=True) and isinstance(data["gate_verdicts"], dict) and len(data["gate_verdicts"]) <= 16 and all(isinstance(k, str) and k in {*("G" + str(n) for n in range(1, 8)), "Q2_ordinary", "Q2_protected"} and v in {"pass", "fail", "n/a"} for k, v in data["gate_verdicts"].items())
    return False


def numeric_map(value, limit, integer=False):
    return isinstance(value, dict) and len(value) <= limit and all(
        isinstance(k, str) and k in {"recall@5", "recall@10", "mrr@10", "hit@10", "temporal_answer", "abstention_precision", "abstention_recall", "queries", "judged", "protected_facets"}
        and (type(v) is int if integer else type(v) in {float, int}) and 0 <= v <= 1000000
        for k, v in value.items())
