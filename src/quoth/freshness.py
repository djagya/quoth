"""Deterministic, privacy-bounded monitoring and explicit offline successor handoff.

No model loading in status; model artifact validation remains refresh's build gate.
Only current versions are evidence: retired historical rows are not erased.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from .assets import eval_path
from .db import connect_read_only
from .refresh import code_digests, db_inventory, inventory, refresh, sha, validate, validate_code_compatibility
from .scopes import load_config
from .vault import IgnoreRules, ignore_rules_sha256, path_excluded


def digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def model_contract() -> str:
    return sha(eval_path("models-manifest.json"))


class Rejected(ValueError):
    def __init__(self, state: str, reason: str):
        self.state, self.reason = state, reason
        super().__init__(reason)


def _require(condition, state, reason):
    if not condition:
        raise Rejected(state, reason)


def _snapshot(db: Path, vault: Path, allow_index_contract_rebase: bool = False) -> tuple[dict, dict]:
    manifest_path = db.with_suffix(".manifest.json")
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw)
    _require(isinstance(manifest, dict), "invalid", "manifest_shape")
    _require(manifest.get("db") == str(db) and manifest.get("db_bytes") == db.stat().st_size
             and manifest.get("db_sha256") == sha(db), "invalid", "snapshot_digest_size")
    _require(manifest.get("status") == "frozen_current_vault_candidate"
             and manifest.get("integrity") == "ok"
             and manifest.get("schema") == "validate_evidence_db", "invalid", "manifest_contract")
    _require("ASSISTANT" in load_config().exclude_dirs, "incompatible", "assistant_scope_required")
    _require(manifest.get("vault_root") == str(vault), "incompatible", "vault_contract")
    _require(isinstance(manifest.get("code_digests"), dict), "incompatible", "code_scope_contract")
    try:
        validate_code_compatibility(manifest, code_digests(),
                                    allow_index_contract_rebase=allow_index_contract_rebase)
    except ValueError:
        raise Rejected("incompatible", "code_scope_contract") from None
    _require(manifest.get("model") == "BAAI/bge-m3"
             and manifest.get("model_manifest_sha256") == model_contract(),
             "incompatible", "model_contract")
    # Reject sidecars: a frozen payload must be fully represented by its main file.
    _require(not any(Path(str(db) + suffix).exists() for suffix in ("-wal", "-journal")),
             "invalid", "unfrozen_sidecar")
    with closing(connect_read_only(db)) as conn:
        _require(conn.execute("PRAGMA query_only").fetchone()[0] == 1, "invalid", "query_only")
        # No row of a note under a built-in exclusion, current or historical: excluded
        # means purged. (`.quothignore` changes make a snapshot stale, not invalid.)
        builtin = IgnoreRules()
        _require(not any(path_excluded(row[0], builtin)
                         for row in conn.execute("SELECT path FROM sources")),
                 "invalid", "excluded_current_evidence")
        counts = validate(conn)
        stored = db_inventory(conn)
        _require(counts == manifest.get("counts"), "invalid", "manifest_counts")
        _require(digest(stored) == manifest.get("source_inventory_sha256"), "invalid", "inventory_digest")
    _require(sha(db) == manifest["db_sha256"] and manifest_path.read_bytes() == raw,
             "invalid", "snapshot_changed_during_check")
    evidence = {"db_sha256": manifest["db_sha256"], "db_bytes": manifest["db_bytes"],
                "manifest_sha256": hashlib.sha256(raw).hexdigest(), "manifest_bytes": len(raw),
                "inventory_sha256": digest(stored), "counts": counts,
                "integrity": "ok", "query_only": True,
                "excluded_current_sources": 0, "excluded_current_chunks": 0,
                "excluded_current_vectors": 0,
                "ignore_rules_sha256": manifest.get("ignore_rules_sha256")}
    return stored, evidence


def status(source: Path, vault: Path, *, allow_index_contract_rebase: bool = False) -> dict:
    """Stable JSON-compatible result; never returns exception text or note paths.

    allow_index_contract_rebase is an explicit operator override for one successor
    build across a declared REBASE_CONTRACT_FILES change; monitoring never sets it.
    """
    try:
        source, vault = source.resolve(), vault.resolve()
        stored, evidence = _snapshot(source, vault, allow_index_contract_rebase)
        current = inventory(vault)
        _require(current == inventory(vault), "invalid", "vault_drift_during_check")
        rules_changed = ignore_rules_sha256(vault) != evidence["ignore_rules_sha256"]
        added, removed = current.keys() - stored.keys(), stored.keys() - current.keys()
        changed = sum(current[p] != stored[p] for p in current.keys() & stored.keys())
        reasons = (["inventory_changed"] if added or removed or changed else []) + (
            ["ignore_rules_changed"] if rules_changed else [])
        stale = bool(reasons)
        return {"state": "stale" if stale else "fresh", "reasons": reasons,
                "delta": {"added": len(added), "removed": len(removed), "changed": changed},
                "current_inventory_sha256": digest(current), "snapshot": evidence}
    except Rejected as exc:
        return {"state": exc.state, "reasons": [exc.reason]}
    except (OSError, ValueError, RuntimeError, sqlite3.Error, KeyError, TypeError):
        return {"state": "invalid", "reasons": ["unreadable_or_invalid_snapshot_vault"]}


def successor(source: Path, target: Path, vault: Path, *, resume=False, **settings) -> dict:
    """Explicit target is the durable operation ID; reuse it on every retry.

    One nonblocking advisory lock serializes cooperating builders, including resume.
    Frozen targets are only checked, never reopened for writing. No cutover occurs.
    """
    source, target, vault = source.resolve(), target.absolute(), vault.resolve()
    prior = status(source, vault,
                   allow_index_contract_rebase=settings.get("allow_index_contract_rebase", False))
    if prior["state"] != "stale":
        return {"status": prior, "action": "none"}
    _require(target.resolve() != source and not target.is_symlink(), "invalid", "target_collision")
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.with_suffix(".lock")
    # Retained lock inode avoids unlink/recreate races between waiters.
    import os
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Rejected("invalid", "candidate_busy") from None
        if not target.with_suffix(".manifest.json").exists():
            refresh(source, target, vault, resume=resume, **settings)
        candidate = status(target, vault)
        _require(candidate["state"] == "fresh", "invalid", "candidate_not_fresh")
        manifest = json.loads(target.with_suffix(".manifest.json").read_text())
        _require(manifest.get("source_db_sha256") == prior["snapshot"]["db_sha256"]
                 and manifest.get("source_manifest_sha256") == prior["snapshot"]["manifest_sha256"],
                 "invalid", "candidate_lineage")
        return {"status": prior, "action": "activation_required", "candidate": str(target),
                "rollback": str(source), "verification": candidate,
                "required_step": "an operator must recheck freshness, accept the candidate, point the serving snapshot at it, and verify the MCP server; no cutover performed"}
