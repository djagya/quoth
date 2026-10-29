"""quoth eval set v1: JSONL rows plus a sibling manifest (see docs/EVAL.md).

Validation is plain Python (no jsonschema dependency); `eval/schema/evalset-v1.schema.json`
describes the same contract for other tools, and a test keeps the two in step.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

SCHEMA = "quoth-evalset/v1"
KINDS = ("fact", "preference", "person", "project_state", "decision", "temporal",
         "knowledge_update", "unanswerable", "episodic", "followup")
ORIGINS = ("real_user", "assistant_query", "authored", "synthetic")
STORES = ("vault", "claims", "owner", "assistant")
PRIVACY = ("public", "private")
ROLES = ("user", "assistant")
# match key -> stores it may be used with
MATCH_KEYS = {
    "path": ("vault",),
    "locator": STORES,
    "claim_text_contains": ("claims",),
    "subject_key": ("claims",),
    "text_contains": STORES,
}
ROW_KEYS = {"id", "question", "lang", "asked_at", "context", "kind", "origin", "expect", "tags", "meta"}
REQUIRED_ROW_KEYS = {"id", "question", "lang", "kind", "origin", "expect"}
EXPECT_KEYS = {"abstain", "sources", "stale", "answer_hint"}
ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
LANG_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{1,8})?$")
MAX_QUESTION = 2048


class EvalSetError(ValueError):
    """Invalid set or manifest; `errors` lists row-scoped codes (ids only, never text)."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors[:10]) + (f" (+{len(errors) - 10} more)" if len(errors) > 10 else ""))


@dataclass
class EvalSet:
    rows: list[dict]
    manifest: dict
    sha256: str
    path: Path | None = None
    errors: list[str] = field(default_factory=list)


def manifest_path(set_path: str | Path) -> Path:
    return Path(set_path).with_suffix(".manifest.json")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _iso_ok(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        if len(value) == 10:
            date.fromisoformat(value)
        else:
            datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _source_errors(rid: str, where: str, sources) -> list[str]:
    if not isinstance(sources, list):
        return [f"{rid}: {where} must be a list"]
    errors = []
    for i, src in enumerate(sources):
        tag = f"{rid}: {where}[{i}]"
        if not isinstance(src, dict) or set(src) != {"store", "match"}:
            errors.append(f"{tag} must be {{store, match}}")
            continue
        if src["store"] not in STORES:
            errors.append(f"{tag}.store unknown")
            continue
        match = src["match"]
        if not isinstance(match, dict) or len(match) != 1:
            errors.append(f"{tag}.match must have exactly one key")
            continue
        (key, value), = match.items()
        if key not in MATCH_KEYS:
            errors.append(f"{tag}.match key unknown")
        elif src["store"] not in MATCH_KEYS[key]:
            errors.append(f"{tag}.match.{key} not valid for store {src['store']}")
        elif not isinstance(value, str) or not value.strip():
            errors.append(f"{tag}.match.{key} must be a non-empty string")
        elif key == "path" and (PurePosixPath(value).is_absolute() or ".." in PurePosixPath(value).parts):
            errors.append(f"{tag}.match.path must be vault-relative")
    return errors


def row_errors(row) -> list[str]:
    """Contract errors of one row; messages carry the row id, never its text."""
    if not isinstance(row, dict):
        return ["row is not an object"]
    rid = row.get("id") if isinstance(row.get("id"), str) and ID_RE.match(row.get("id", "")) else "?"
    errors = []
    missing = REQUIRED_ROW_KEYS - set(row)
    extra = set(row) - ROW_KEYS
    if missing:
        errors.append(f"{rid}: missing {sorted(missing)}")
    if extra:
        errors.append(f"{rid}: unknown keys {sorted(extra)}")
    if rid == "?":
        errors.append("row id must match [A-Za-z0-9._-]{1,128}")
    q = row.get("question")
    if not isinstance(q, str) or not 1 <= len(q.strip()) <= MAX_QUESTION:
        errors.append(f"{rid}: question must be 1..{MAX_QUESTION} chars")
    if not isinstance(row.get("lang"), str) or not LANG_RE.match(row.get("lang", "")):
        errors.append(f"{rid}: lang must be a language code")
    if "asked_at" in row and row["asked_at"] is not None and not _iso_ok(row["asked_at"]):
        errors.append(f"{rid}: asked_at must be an ISO date or datetime")
    if "context" in row:
        ctx = row["context"]
        if not isinstance(ctx, list) or any(
                not isinstance(t, dict) or set(t) != {"role", "text"} or t["role"] not in ROLES
                or not isinstance(t["text"], str) or not t["text"].strip() for t in ctx):
            errors.append(f"{rid}: context must be a list of {{role: user|assistant, text}}")
    if row.get("kind") not in KINDS:
        errors.append(f"{rid}: kind unknown")
    if row.get("origin") not in ORIGINS:
        errors.append(f"{rid}: origin unknown")
    if "tags" in row and (not isinstance(row["tags"], list)
                          or any(not isinstance(t, str) or not t for t in row["tags"])):
        errors.append(f"{rid}: tags must be a list of strings")
    if "meta" in row and not isinstance(row["meta"], dict):
        errors.append(f"{rid}: meta must be an object")
    expect = row.get("expect")
    if not isinstance(expect, dict):
        errors.append(f"{rid}: expect must be an object")
        return errors
    if set(expect) - EXPECT_KEYS or not {"abstain", "sources"} <= set(expect):
        errors.append(f"{rid}: expect must have abstain, sources and only stale/answer_hint besides")
    if not isinstance(expect.get("abstain"), bool):
        errors.append(f"{rid}: expect.abstain must be a boolean")
    errors += _source_errors(rid, "expect.sources", expect.get("sources"))
    if "stale" in expect:
        errors += _source_errors(rid, "expect.stale", expect["stale"])
    if "answer_hint" in expect and expect["answer_hint"] is not None and not isinstance(expect["answer_hint"], str):
        errors.append(f"{rid}: expect.answer_hint must be a string or null")
    if isinstance(expect.get("abstain"), bool) and isinstance(expect.get("sources"), list):
        if expect["abstain"] and expect["sources"]:
            errors.append(f"{rid}: an abstain row has no expected sources")
        if not expect["abstain"] and not expect["sources"]:
            errors.append(f"{rid}: an answerable row needs at least one expected source")
    if row.get("kind") == "unanswerable" and expect.get("abstain") is not True:
        errors.append(f"{rid}: unanswerable rows abstain")
    if row.get("kind") == "followup" and not row.get("context"):
        errors.append(f"{rid}: followup rows need context")
    return errors


def counts(rows: list[dict]) -> dict:
    return {
        "kind": dict(sorted(Counter(r["kind"] for r in rows).items())),
        "origin": dict(sorted(Counter(r["origin"] for r in rows).items())),
        "lang": dict(sorted(Counter(r["lang"] for r in rows).items())),
        "abstain": {"true": sum(bool(r["expect"]["abstain"]) for r in rows),
                    "false": sum(not r["expect"]["abstain"] for r in rows)},
    }


def build_manifest(rows: list[dict], data: bytes, *, privacy: str, fixture: str,
                   source: dict | None = None) -> dict:
    if privacy not in PRIVACY:
        raise ValueError("privacy must be public or private")
    out = {"schema": SCHEMA, "privacy": privacy, "sha256": sha256_bytes(data), "rows": len(rows),
           "counts": counts(rows), "fixture": fixture}
    if source:
        out["source"] = source
    return out


def dumps_rows(rows: list[dict]) -> bytes:
    return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows).encode("utf-8")


def write_set(rows: list[dict], out: str | Path, *, privacy: str, fixture: str,
              source: dict | None = None) -> dict:
    """Validate, write `out` (JSONL) and its manifest; returns the manifest."""
    errors = [e for r in rows for e in row_errors(r)] + _set_errors(rows, privacy)
    if errors:
        raise EvalSetError(errors)
    data = dumps_rows(rows)
    manifest = build_manifest(rows, data, privacy=privacy, fixture=fixture, source=source)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    manifest_path(out).write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                                  encoding="utf-8")
    if privacy == "private":
        for p in (out, manifest_path(out)):
            p.chmod(0o600)
    return manifest


def _set_errors(rows: list[dict], privacy: str | None) -> list[str]:
    errors = []
    ids = [r.get("id") for r in rows if isinstance(r, dict)]
    dup = sorted({i for i in ids if ids.count(i) > 1 and isinstance(i, str)})
    if dup:
        errors.append(f"duplicate ids {dup[:5]}")
    if not rows:
        errors.append("set is empty")
    if privacy == "public":
        for r in rows:
            if isinstance(r, dict) and isinstance(r.get("expect"), dict) and r["expect"].get("answer_hint"):
                errors.append(f"{r.get('id')}: answer_hint is for private sets only")
    return errors


def _manifest_errors(manifest, rows: list[dict], digest: str) -> list[str]:
    if not isinstance(manifest, dict):
        return ["manifest is not an object"]
    errors = []
    if manifest.get("schema") != SCHEMA:
        errors.append("manifest schema is not " + SCHEMA)
    if manifest.get("privacy") not in PRIVACY:
        errors.append("manifest privacy must be public or private")
    if manifest.get("sha256") != digest:
        errors.append("manifest sha256 does not match the set")
    if manifest.get("rows") != len(rows):
        errors.append("manifest row count does not match the set")
    if not isinstance(manifest.get("fixture"), str) or not manifest.get("fixture"):
        errors.append("manifest fixture missing")
    try:
        if manifest.get("counts") != counts(rows):
            errors.append("manifest counts do not match the set")
    except (KeyError, TypeError):
        pass  # row errors already cover malformed rows
    return errors


def validate(set_path: str | Path) -> list[str]:
    """All contract errors of a set file and its manifest ([] when valid)."""
    path = Path(set_path)
    try:
        raw = path.read_bytes()
    except OSError:
        return ["set file unreadable"]
    rows, errors = [], []
    for n, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            errors.append(f"line {n}: invalid JSON")
    for r in rows:
        errors += row_errors(r)
    mpath = manifest_path(path)
    try:
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return errors + _set_errors(rows, None) + ["manifest missing or invalid JSON"]
    errors += _set_errors(rows, manifest.get("privacy") if isinstance(manifest, dict) else None)
    return errors + _manifest_errors(manifest, rows, sha256_bytes(raw))


def load_set(set_path: str | Path) -> EvalSet:
    """Load and validate a set; raises EvalSetError listing every problem."""
    errors = validate(set_path)
    if errors:
        raise EvalSetError(errors)
    path = Path(set_path)
    raw = path.read_bytes()
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    manifest = json.loads(manifest_path(path).read_text(encoding="utf-8"))
    return EvalSet(rows=rows, manifest=manifest, sha256=sha256_bytes(raw), path=path)


def vault_paths(row: dict) -> list[str]:
    """Expected vault paths of a row, in order (golden-v1 `expected_paths`)."""
    return [s["match"]["path"] for s in row["expect"]["sources"]
            if s["store"] == "vault" and "path" in s["match"]]
