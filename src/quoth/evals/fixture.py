"""Build a runnable fixture (snapshot, ledger, state.db, episode lanes) from plain-text sources.

A fixture directory (``eval/public`` is the shipped one) holds only text:

- ``vault/``              Markdown notes, indexed by the normal ingest + embed path
- ``ledger-seed.jsonl``   ``{text, kind, status, subject_key?, origin?, locator?}`` applied in
                          order (``accepted`` / ``pending`` / ``rejected``; same subject_key
                          accepted later supersedes the earlier claim)
- ``telegram.jsonl``      ``{id, session, source, role, ts, text}`` Hermes messages, written
                          into a ``state.db`` with Hermes' schema by `build_state_db`
- ``set.jsonl``           the eval set (+ ``set.manifest.json``)

`build_fixture` writes the binaries into a work directory, never into the source tree.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

DEFAULT_MODEL = "hash-ngram"
STATE_SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT,
    parent_session_id TEXT, profile_name TEXT, user_id TEXT);
CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL,
    active INTEGER NOT NULL, timestamp REAL NOT NULL, content TEXT, display_kind TEXT,
    platform_message_id TEXT, compacted INTEGER NOT NULL DEFAULT 0);
"""


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def build_snapshot(vault: Path, target: Path, model: str = DEFAULT_MODEL) -> Path:
    """Ingest + embed a vault into a frozen snapshot with a valid serving manifest."""
    from ..db import open_db
    from ..embed.backends import get_embedder
    from ..embed.store import embed_chunks
    from ..ingest import ingest_vault
    from ..refresh import sha

    target.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(target)
    try:
        stats = ingest_vault(conn, vault)
        if stats.errors:
            raise RuntimeError("fixture vault ingest failed")
        embed_chunks(conn, get_embedder(model, allow_download=False), batch_size=16)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
    finally:
        conn.close()
    resolved = target.resolve()
    target.with_suffix(".manifest.json").write_text(json.dumps({
        "db": str(resolved), "db_sha256": sha(resolved), "db_bytes": resolved.stat().st_size,
        "status": "frozen_current_vault_candidate", "integrity": "ok", "schema": "validate_evidence_db",
        "vault_root": str(vault.resolve())}, indent=2))
    return target


def build_ledger(seed: list[dict], target: Path) -> dict:
    from ..ledger import core

    conn = core.open_ledger(target)
    counts = {"accepted": 0, "pending": 0, "rejected": 0}
    try:
        for row in seed:
            origin = row.get("origin", "assistant_proposal")
            result = core.propose(conn, text=row["text"], kind=row["kind"], origin=origin,
                                  proposer="fixture", reason="synthetic fixture",
                                  evidence_locator=row.get("locator"), subject_key=row.get("subject_key"))
            status = row.get("status", "accepted")
            cid = result["claim"]["id"]
            if status == "accepted":
                core.accept(conn, cid, actor="fixture")
            elif status == "rejected":
                core.reject(conn, cid, actor="fixture")
            counts[status] += 1
    finally:
        conn.close()
    return counts


def build_state_db(messages: list[dict], target: Path) -> Path:
    """A Hermes-shaped state.db holding only the given synthetic messages."""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    db = sqlite3.connect(target)
    try:
        db.executescript(STATE_SCHEMA)
        sessions = {}
        for m in messages:
            sessions.setdefault(m["session"], m.get("source", "telegram"))
        db.executemany("INSERT INTO sessions(id, source) VALUES (?, ?)", sorted(sessions.items()))
        db.executemany(
            "INSERT INTO messages(id, session_id, role, active, compacted, timestamp, content)"
            " VALUES (?,?,?,?,?,?,?)",
            [(m["id"], m["session"], m["role"], int(m.get("active", 1)), int(m.get("compacted", 0)),
              float(m["ts"]), m["text"]) for m in messages])
        db.commit()
    finally:
        db.close()
    return target


def build_lane(state_db: Path, target: Path, role: str, model: str = DEFAULT_MODEL) -> dict:
    from ..episodes import Episodes

    eps = Episodes(target, model=model, role=role)
    try:
        out = eps.ingest(state_db)
        while eps.embed(64):
            pass
        return {"messages": out["added"], "pending": eps.pending()}
    finally:
        eps.close()


def build_fixture(source: str | Path, out: str | Path, model: str = DEFAULT_MODEL) -> dict:
    """Build every store of a fixture directory into `out`; returns the paths (for `eval run`)."""
    source, out = Path(source), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    paths = {"db": out / "snapshot.db", "ledger": out / "ledger.db", "state_db": out / "state.db",
             "episodes": out / "episodes.db", "owner_episodes": out / "owner-episodes.db"}
    for p in paths.values():
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(p) + suffix).unlink(missing_ok=True)
    build_snapshot(source / "vault", paths["db"], model)
    summary = {"ledger": build_ledger(_jsonl(source / "ledger-seed.jsonl"), paths["ledger"])}
    build_state_db(_jsonl(source / "telegram.jsonl"), paths["state_db"])
    summary["assistant"] = build_lane(paths["state_db"], paths["episodes"], "assistant", model)
    summary["owner"] = build_lane(paths["state_db"], paths["owner_episodes"], "owner", model)
    result = {"paths": {k: str(v) for k, v in paths.items()}, "model": model, "built": summary}
    (out / "fixture.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result
