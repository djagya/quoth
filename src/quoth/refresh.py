"""Offline, resumable refresh of a frozen vault-only dense evidence snapshot."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from .db import connect_read_only, list_migrations, open_db, validate_evidence_db
from . import paths
from .embed.backends import MODELS, get_embedder
from .embed.store import embed_chunks
from .ingest import ingest_vault
from .assets import eval_path
from .scopes import SCOPES
from .vault import IgnoreRules, frontmatter_excluded, ignore_rules_sha256, walk

# Every snapshot manifest carries the digests of each ingestion/embedding contract file.
CODE_FILES = ("chunker.py", "db.py", "dense.py", "embed/backends.py", "embed/store.py",
              "entities.py", "ingest.py", "links.py", "scopes.py", "scopes.toml", "search.py",
              "vault.py")
SCOPE_CONTRACT_FILES = frozenset(("scopes.py", "scopes.toml"))
SERVING_CONTRACT_FILES = frozenset(("search.py",))
# Index-contract files whose changes an operator may explicitly declare to preserve
# already stored chunks and vectors (vector reuse, retired-vector pruning, embedding
# order, asset lookup). Chunking and the embedder input (chunker.passage_text),
# ingestion, scopes, schema, entities and the embedder itself are never rebased:
# changing them requires rebuilding the affected data.
REBASE_CONTRACT_FILES = frozenset(("embed/store.py", "refresh.py", "freshness.py"))


def index_code_digests() -> dict[str, str]:
    """Persisted-data contract; retrieval-only search/pipeline are not builders."""
    root = Path(__file__).parent
    return {k: v for k, v in code_digests().items() if k not in SERVING_CONTRACT_FILES} | {
        name: sha(root / name) for name in ("refresh.py", "freshness.py")
    }


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for buf in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(buf)
    return h.hexdigest()


def code_digests() -> dict[str, str]:
    root = Path(__file__).parent
    return {name: sha(root / name) for name in CODE_FILES} | {
        "migrations/" + name: hashlib.sha256(sql.encode()).hexdigest()
        for _, name, sql in list_migrations()
    }


def validate_code_compatibility(source_manifest: dict, current: dict[str, str],
                                *, allow_scope_contract_change: bool = False,
                                allow_index_contract_rebase: bool = False) -> None:
    allowed = ((SCOPE_CONTRACT_FILES if allow_scope_contract_change else frozenset())
               | (REBASE_CONTRACT_FILES if allow_index_contract_rebase else frozenset()))
    source = source_manifest.get("code_digests")
    if not isinstance(source, dict) or set(source) != set(current):
        raise ValueError("source code compatibility mismatch")
    index = source_manifest.get("index_code_digests")
    if index is not None:
        expected = index_code_digests()
        if not isinstance(index, dict) or set(index) != set(expected):
            raise ValueError("source index compatibility mismatch")
        index_changed = {name for name in expected if index[name] != expected[name]}
        if index_changed and not index_changed <= allowed:
            raise ValueError("source index compatibility mismatch")
    # Pre-split manifests attest the existing index files, but not refresh itself.
    # Retain their complete key set; only the audited serving file may differ.
    changed = {name for name in set(source) | set(current)
               if source.get(name) != current.get(name) and name not in SERVING_CONTRACT_FILES}
    if not changed or changed <= allowed:
        return
    raise ValueError("source code compatibility mismatch")


def inventory(vault: Path) -> dict[str, str]:
    """{path: sha256} of every note that would be indexed, by the rules ingest applies.

    An unreadable folder or a non-regular note raises: a partial scan must never be
    mistaken for deleted notes or a complete fresh inventory.
    """
    if not vault.is_dir():
        raise ValueError(f"vault directory not found: {vault}")
    result = {}
    for entry in walk(vault, IgnoreRules.load(vault), strict=True):
        if entry.excluded:
            continue
        raw = entry.path.read_bytes()
        if not frontmatter_excluded(raw):
            result[entry.rel] = hashlib.sha256(raw).hexdigest()
    return result


def vault_state(vault: Path) -> tuple[dict[str, str], str | None]:
    """The inventory and the ``.quothignore`` digest: both define what a snapshot holds."""
    return inventory(vault), ignore_rules_sha256(vault)


def db_inventory(conn) -> dict[str, str]:
    rows = conn.execute("SELECT s.path, v.content_hash FROM sources s JOIN source_versions v "
                        "ON v.source_id=s.id AND v.is_current=1").fetchall()
    result = {row[0]: row[1] for row in rows}
    if len(result) != len(rows):
        raise ValueError("duplicate current vault versions")
    return result


def validate(conn, expected: dict[str, str] | None = None) -> dict[str, int]:
    if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ValueError("SQLite integrity_check failed")
    if conn.execute("PRAGMA foreign_key_check").fetchone():
        raise ValueError("SQLite foreign keys invalid")
    validate_evidence_db(conn)
    if conn.execute("SELECT 1 FROM embeddings WHERE model!='bge-m3' LIMIT 1").fetchone():
        raise ValueError("unexpected embedding model")
    if conn.execute(f"SELECT 1 FROM sources WHERE scope NOT IN ({','.join('?' * len(SCOPES))})"
                    " LIMIT 1", SCOPES).fetchone():
        raise ValueError("invalid vault scope")
    if expected is not None and db_inventory(conn) != expected:
        raise ValueError("vault inventory differs from current DB paths/hashes")
    current = conn.execute("SELECT count(*) FROM chunks c JOIN source_versions v "
                           "ON v.id=c.source_version_id AND v.is_current=1").fetchone()[0]
    vectors = conn.execute("SELECT count(*) FROM chunks c JOIN source_versions v "
                           "ON v.id=c.source_version_id AND v.is_current=1 "
                           "JOIN embeddings e ON e.chunk_id=c.id AND e.model='bge-m3' "
                           "AND e.dim=1024 AND length(e.vector)=4096").fetchone()[0]
    if current != vectors:
        raise ValueError(f"current bge-m3 vector coverage: {vectors}/{current}")
    return {"notes_current": len(db_inventory(conn)), "current_chunks": current,
            "current_bge_m3_vectors": vectors}


def _json_write(path: Path, obj: dict, *, exclusive: bool = True) -> None:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())


def _model_digest() -> str:
    manifest = eval_path("models-manifest.json")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if data.get("model") != MODELS["bge-m3"].source_id:
        raise ValueError("model manifest mismatch")
    root = paths.model_cache() / MODELS["bge-m3"].source_id
    for item in data["files"]:
        path = root / item["file"]
        if not path.is_file() or path.stat().st_size != item["bytes"] or sha(path) != item["sha256"]:
            raise ValueError(f"model file does not match manifest: {item['file']}")
    return sha(manifest)


def prune_retired_vectors(conn) -> int:
    """Vectors are derived data: drop those of retired versions; chunks and versions stay."""
    return conn.execute(
        "DELETE FROM embeddings WHERE chunk_id IN (SELECT c.id FROM chunks c"
        " JOIN source_versions v ON v.id = c.source_version_id WHERE v.is_current = 0)").rowcount


def refresh(source: Path, target: Path, vault: Path, *, resume: bool = False,
            retries: int = 2, threads: int = 4, batch_size: int = 32, embedder=None,
            allow_scope_contract_change: bool = False,
            allow_index_contract_rebase: bool = False) -> dict:
    """Freeze a new target or resume only an owned incomplete target. Never alter source."""
    source, target, vault = source.resolve(), target.absolute(), vault.resolve()
    if source == target or target.is_symlink() or not source.is_file():
        raise ValueError("source/target invalid")
    if retries < 0 or batch_size < 1 or threads < 1:
        raise ValueError("invalid retry or embedding settings")
    manifest_path = source.with_suffix(".manifest.json")
    output_manifest = target.with_suffix(".manifest.json")
    checkpoint = target.with_suffix(".checkpoint.json")
    if output_manifest.exists() or output_manifest.is_symlink():
        raise ValueError("frozen output manifest already exists")
    source_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_hash = sha(source)
    if (source_manifest.get("db") != str(source) or source_manifest.get("db_sha256") != source_hash
            or source_manifest.get("db_bytes") != source.stat().st_size
            or source_manifest.get("status") != "frozen_current_vault_candidate"
            or source_manifest.get("vault_root") != str(vault)
            or source_manifest.get("model") != "BAAI/bge-m3"
            or source_manifest.get("integrity") != "ok"
            or source_manifest.get("schema") != "validate_evidence_db"):
        raise ValueError("incompatible source manifest/hash")
    digests = code_digests()
    validate_code_compatibility(source_manifest, digests,
                                allow_scope_contract_change=allow_scope_contract_change,
                                allow_index_contract_rebase=allow_index_contract_rebase)
    model_digest = _model_digest() if embedder is None else sha(eval_path("models-manifest.json"))
    if source_manifest.get("model_manifest_sha256", model_digest) != model_digest:
        raise ValueError("source model compatibility mismatch")
    with closing(connect_read_only(source)) as src:
        counts = validate(src)
        if counts != {k: source_manifest["counts"][k] for k in counts}:
            raise ValueError("source manifest counts mismatch")
        if source_manifest.get("source_inventory_sha256") is None:
            raise ValueError("missing source inventory digest")
        if source_manifest["source_inventory_sha256"] != hashlib.sha256(
            json.dumps(db_inventory(src), sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest():
            raise ValueError("source inventory digest mismatch")
        if not resume:
            if target.exists() or checkpoint.exists() or target.with_name(target.name + ".copying").exists():
                raise ValueError("output target collision")
            target.parent.mkdir(parents=True, exist_ok=True)
            _json_write(checkpoint, {"source_sha256": source_hash, "target": str(target),
                                     "code_digests": digests, "index_code_digests": index_code_digests(),
                                     "model_manifest_sha256": model_digest})
            copying = target.with_name(target.name + ".copying")
            try:
                with closing(sqlite3.connect(copying)) as dest:
                    src.backup(dest)
                os.replace(copying, target)
            finally:
                copying.unlink(missing_ok=True)
        else:
            if not checkpoint.is_file() or json.loads(checkpoint.read_text()) != {
                "source_sha256": source_hash, "target": str(target),
                "code_digests": digests, "index_code_digests": index_code_digests(),
                "model_manifest_sha256": model_digest
            } or not target.is_file():
                raise ValueError("no compatible resumable checkpoint")
    return _build_and_freeze(target, vault, output_manifest, checkpoint, retries=retries,
                             threads=threads, batch_size=batch_size, embedder=embedder,
                             digests=digests, model_digest=model_digest, source_hash=source_hash,
                             source_manifest_sha256=sha(manifest_path))


def _build_and_freeze(target: Path, vault: Path, output_manifest: Path, checkpoint: Path, *,
                      retries: int, threads: int, batch_size: int, embedder, digests: dict,
                      model_digest: str, source_hash: str | None,
                      source_manifest_sha256: str | None) -> dict:
    """Ingest and embed the vault into ``target``, validate it, and write the frozen manifest."""
    try:
        with closing(sqlite3.connect(target, isolation_level=None)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            for attempt in range(retries + 1):
                before, rules_before = vault_state(vault)
                stats = ingest_vault(conn, vault)
                if stats.errors:
                    raise ValueError(f"vault ingest errors: {stats.errors}")
                if embedder is None:
                    embedder = get_embedder("bge-m3", threads=threads)
                if embedder.spec.name != "bge-m3" or embedder.spec.dim != 1024:
                    raise ValueError("requires bge-m3 embedder")
                embed_stats = embed_chunks(conn, embedder, batch_size=batch_size)
                after, rules_after = vault_state(vault)
                if before == after and rules_before == rules_after and db_inventory(conn) == after:
                    pruned = prune_retired_vectors(conn)
                    counts = validate(conn, after)
                    # Close and reopen below before hashing: no live WAL pages in frozen payload.
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    conn.execute("PRAGMA journal_mode=DELETE")
                    conn.execute("VACUUM")
                    break
            else:
                raise ValueError("vault source drift: retry budget exhausted; resume checkpoint")
        with closing(connect_read_only(target)) as frozen:
            if frozen.execute("PRAGMA query_only").fetchone()[0] != 1:
                raise ValueError("read-only evidence connection unavailable")
            if vault_state(vault) != (db_inventory(frozen), rules_after):
                raise ValueError("vault drift after checkpoint; resume checkpoint")
            validate(frozen, db_inventory(frozen))
        payload = {"status": "frozen_current_vault_candidate", "db": str(target),
                   "built_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "ignore_rules_sha256": rules_after,
                   "db_bytes": target.stat().st_size, "db_sha256": sha(target),
                   "source_db_sha256": source_hash, "source_manifest_sha256": source_manifest_sha256,
                   "source_inventory_sha256": hashlib.sha256(json.dumps(after, sort_keys=True,
                       ensure_ascii=False).encode()).hexdigest(),
                   "code_digests": digests, "index_code_digests": index_code_digests(),
                   "serving_code_digests": {name: digests[name] for name in SERVING_CONTRACT_FILES},
                   "model_manifest_sha256": model_digest,
                   "vault_root": str(vault), "model": "BAAI/bge-m3", "counts": counts,
                   "integrity": "ok", "schema": "validate_evidence_db",
                   "mcp_tools": ["evidence_bundle"], "read_only_query_only": True,
                   "ingest": stats.as_dict(), "embed": embed_stats.as_dict(),
                   "pruned_retired_vectors": pruned,
                   "scope": "vault-only; no sessions or live activation"}
        _json_write(output_manifest, payload)
        os.chmod(target, 0o444)
        os.chmod(output_manifest, 0o444)
        checkpoint.unlink()
        return payload
    except BaseException:
        # The checkpoint and every committed batch survive errors and interruption.
        raise


def snapshot(target: Path, vault: Path, *, resume: bool = False, retries: int = 2,
             threads: int = 4, batch_size: int = 32, embedder=None) -> dict:
    """Build a first frozen snapshot of ``vault`` from scratch at ``target``.

    The result has the same manifest contract as a refresh output, so it can be served,
    checked with ``quoth freshness`` and used as the source of later refreshes. With
    ``resume``, an interrupted build continues from the checkpoint beside ``target``
    (it must name this target and vault and match the current code and model digests):
    committed chunks and vectors are kept, and only what is missing is embedded.
    """
    target, vault = target.absolute(), vault.resolve()
    if retries < 0 or batch_size < 1 or threads < 1:
        raise ValueError("invalid retry or embedding settings")
    output_manifest = target.with_suffix(".manifest.json")
    checkpoint = target.with_suffix(".checkpoint.json")
    digests = code_digests()
    model_digest = _model_digest() if embedder is None else sha(eval_path("models-manifest.json"))
    expected = {"source_sha256": None, "target": str(target), "vault_root": str(vault),
                "code_digests": digests, "index_code_digests": index_code_digests(),
                "model_manifest_sha256": model_digest}
    copying = target.with_name(target.name + ".copying")
    if output_manifest.exists() or output_manifest.is_symlink() or target.is_symlink():
        raise ValueError("output target collision")
    if not resume:
        if target.exists() or checkpoint.exists() or copying.exists():
            raise ValueError("output target collision")
        target.parent.mkdir(parents=True, exist_ok=True)
        _json_write(checkpoint, expected)
    else:
        if checkpoint.is_symlink() or not checkpoint.is_file():
            raise ValueError("no compatible resumable checkpoint")
        try:
            saved = json.loads(checkpoint.read_text(encoding="utf-8"))
        except ValueError:
            raise ValueError("no compatible resumable checkpoint") from None
        if saved != expected or (target.exists() and not target.is_file()):
            raise ValueError("no compatible resumable checkpoint")
    # Creates the file and applies every migration; a no-op on a resumed target.
    open_db(target).close()
    return _build_and_freeze(target, vault, output_manifest, checkpoint, retries=retries,
                             threads=threads, batch_size=batch_size, embedder=embedder,
                             digests=digests, model_digest=model_digest, source_hash=None,
                             source_manifest_sha256=None)
