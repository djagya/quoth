"""Converters from the pre-v1 formats into eval set v1, and back to their expected paths.

- ``golden-v1``: ``{id, question, lang, expected_paths, scope, kind}`` (eval/golden.jsonl and
  a private golden-v1.jsonl, used by the nightly gate's ``--golden``).
- ``golden-v3``: the frozen private-v3 package rows (``V3_KEYS``).
- ``ledger-updates``: knowledge-update steps plus ``query``, ``current`` and ``stale``; the
  steps become a ledger seed for the fixture, the query a ``knowledge_update`` row.

Nothing here prints row content. Private input stays private: the output inherits
``--privacy private`` by default and is written mode 0600.
"""
from __future__ import annotations

import json
from pathlib import Path

from .format import KINDS, sha256_bytes, vault_paths, write_set

FORMATS = ("golden-v1", "golden-v3", "ledger-updates")
V1_KEYS = {"id", "question", "lang", "expected_paths", "kind"}
V3_META = ("domain", "post_snapshot", "source_kind", "temporal_kind", "expected_event")
V3_KEYS = {"id", "question", "lang", "kind", "expected_paths", "answer_hint", "abstain", "asked_at",
           "origin", *V3_META}
V3_PACKAGE_SCHEMA = "quoth-private-golden-package/v1"
KIND_MAP = {"project": "project_state", "project-state": "project_state", "adversarial": "unanswerable"}
ORIGINS_V3 = ("real_user", "assistant_query", "authored")


class ConvertError(ValueError):
    pass


def _read_jsonl(path: Path) -> tuple[list[dict], bytes]:
    raw = path.read_bytes()
    try:
        rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConvertError("input is not JSONL") from exc
    if any(not isinstance(r, dict) for r in rows):
        raise ConvertError("input rows must be objects")
    return rows, raw


def _kind(legacy: str) -> tuple[str, list[str]]:
    kind = KIND_MAP.get(legacy, legacy)
    if kind not in KINDS:
        kind = "fact"
    return kind, ([] if kind == legacy else [f"legacy_kind:{legacy}"])


def _vault_sources(paths: list[str]) -> list[dict]:
    return [{"store": "vault", "match": {"path": p}} for p in paths]


def from_golden_v1(rows: list[dict], origin: str = "authored") -> list[dict]:
    out = []
    for r in rows:
        if not V1_KEYS <= set(r):
            raise ConvertError(f"{r.get('id', '?')}: not a golden-v1 row")
        kind, tags = _kind(str(r["kind"]))
        if "scope" in r:
            tags.append(f"scope:{r['scope']}")
        paths = list(r["expected_paths"])
        out.append({"id": r["id"], "question": r["question"], "lang": r["lang"], "kind": kind,
                    "origin": origin, "tags": tags,
                    "expect": {"abstain": not paths, "sources": _vault_sources(paths)}})
    return out


def to_golden_v1(row: dict) -> dict:
    """Inverse of `from_golden_v1` (exact for converted rows)."""
    tags = row.get("tags", [])
    legacy = next((t.split(":", 1)[1] for t in tags if t.startswith("legacy_kind:")), row["kind"])
    out = {"id": row["id"], "question": row["question"], "lang": row["lang"],
           "expected_paths": vault_paths(row), "kind": legacy}
    scope = next((t.split(":", 1)[1] for t in tags if t.startswith("scope:")), None)
    if scope is not None:
        out["scope"] = scope
    return out


def from_golden_v3(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        if set(r) != V3_KEYS:
            raise ConvertError(f"{r.get('id', '?')}: not a golden-v3 row")
        if r["origin"] not in ORIGINS_V3:
            raise ConvertError(f"{r['id']}: unknown golden-v3 origin")
        kind, tags = _kind(str(r["kind"]))
        expect = {"abstain": bool(r["abstain"]), "sources": _vault_sources(list(r["expected_paths"]))}
        if r["answer_hint"] is not None:
            expect["answer_hint"] = r["answer_hint"]
        out.append({"id": r["id"], "question": r["question"], "lang": r["lang"], "kind": kind,
                    "origin": r["origin"], "asked_at": r["asked_at"], "tags": tags,
                    "meta": {k: r[k] for k in V3_META}, "expect": expect})
    return out


def to_golden_v3(row: dict) -> dict:
    """Inverse of `from_golden_v3` (exact for converted rows)."""
    tags = row.get("tags", [])
    legacy = next((t.split(":", 1)[1] for t in tags if t.startswith("legacy_kind:")), row["kind"])
    meta = row.get("meta", {})
    return {"id": row["id"], "question": row["question"], "lang": row["lang"], "kind": legacy,
            "expected_paths": vault_paths(row), "answer_hint": row["expect"].get("answer_hint"),
            "abstain": row["expect"]["abstain"], "asked_at": row.get("asked_at"),
            "origin": row["origin"], **{k: meta.get(k) for k in V3_META}}


def from_ledger_updates(rows: list[dict], origin: str = "synthetic") -> tuple[list[dict], list[dict]]:
    """(eval rows, ledger seed). Seed rows are applied in order and accepted (latest wins)."""
    out, seed = [], []
    for r in rows:
        if not {"id", "steps", "query", "current", "stale"} <= set(r):
            raise ConvertError(f"{r.get('id', '?')}: not a ledger-updates row")
        for step in r["steps"]:
            seed.append({"text": step["text"], "kind": step["kind"], "subject_key": step.get("subject_key"),
                         "status": "accepted", "row": r["id"]})
        key = next((s.get("subject_key") for s in reversed(r["steps"]) if s.get("subject_key")), None)
        tags = [f"subject_key:{key}"] if key else []
        out.append({"id": r["id"], "question": r["query"], "lang": r.get("lang", "en"),
                    "kind": "knowledge_update", "origin": origin, "tags": tags,
                    "expect": {"abstain": False,
                               "sources": [{"store": "claims", "match": {"claim_text_contains": r["current"]}}],
                               "stale": [{"store": "claims", "match": {"claim_text_contains": s}}
                                         for s in r["stale"]]}})
    return out, seed


def _v3_package_check(path: Path, raw: bytes) -> None:
    """When the private package manifest sits next to the input, its sha256 must match."""
    manifest = path.parent / "manifest.json"
    if not manifest.is_file():
        return
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if data.get("schema") == V3_PACKAGE_SCHEMA:
        expected = (data.get("dataset") or {}).get("sha256")
        if expected and expected != sha256_bytes(raw):
            raise ConvertError("golden-v3 input does not match its package manifest sha256")


def read_jsonl(path: str | Path) -> list[dict]:
    """Rows of a pre-v1 JSONL file (golden-v1, the private v2 set); replaces `eval.load_golden`."""
    return _read_jsonl(Path(path))[0]


def convert_in_memory(fmt: str, src: str | Path, *, origin: str | None = None):
    """Convert a golden-v1/golden-v3 file into an `EvalSet` without writing anything.

    For the nightly gate's deprecated ``--golden`` / ``--quality-golden-v3`` flags: the set
    is validated as eval set v1 and its manifest records the source format and sha256, as
    `eval convert` would write it. Private by definition (the owner's own data).
    """
    from .format import EvalSet, EvalSetError, _set_errors, build_manifest, dumps_rows, row_errors

    if fmt not in ("golden-v1", "golden-v3"):
        raise ConvertError(f"cannot convert {fmt} in memory")
    src = Path(src)
    rows, raw = _read_jsonl(src)
    if fmt == "golden-v1":
        converted = from_golden_v1(rows, origin=origin or "authored")
    else:
        _v3_package_check(src, raw)
        converted = from_golden_v3(rows)
    errors = [e for r in converted for e in row_errors(r)] + _set_errors(converted, "private")
    if errors:
        raise EvalSetError(errors)
    data = dumps_rows(converted)
    manifest = build_manifest(converted, data, privacy="private", fixture="private",
                              source={"format": fmt, "sha256": sha256_bytes(raw)})
    return EvalSet(rows=converted, manifest=manifest, sha256=manifest["sha256"], path=src)


def convert(fmt: str, src: str | Path, out: str | Path, *, privacy: str = "private",
            fixture: str = "private", origin: str | None = None, seed_out: str | Path | None = None) -> dict:
    """Convert `src` into eval set v1 at `out` (+ manifest). Returns a content-free summary."""
    if fmt not in FORMATS:
        raise ConvertError(f"unknown format {fmt}")
    src = Path(src)
    rows, raw = _read_jsonl(src)
    seed = None
    if fmt == "golden-v1":
        converted = from_golden_v1(rows, origin=origin or "authored")
    elif fmt == "golden-v3":
        _v3_package_check(src, raw)
        converted = from_golden_v3(rows)
    else:
        converted, seed = from_ledger_updates(rows, origin=origin or "synthetic")
    manifest = write_set(converted, out, privacy=privacy, fixture=fixture,
                         source={"format": fmt, "sha256": sha256_bytes(raw)})
    summary = {"rows": manifest["rows"], "sha256": manifest["sha256"], "counts": manifest["counts"]}
    if seed is not None:
        seed_path = Path(seed_out) if seed_out else Path(out).with_suffix(".ledger-seed.jsonl")
        seed_path.write_text("".join(json.dumps(s, ensure_ascii=False) + "\n" for s in seed), encoding="utf-8")
        summary["ledger_seed_rows"] = len(seed)
    return summary
