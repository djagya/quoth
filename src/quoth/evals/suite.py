"""Eval suites: one file that lists sets and what each is for, run against one target.

A suite lets public and personal sets run the same way. Each entry names a set (`path`,
relative to the suite file, or `builtin`, a set shipped under `eval/sets/`) and a role:

- `smoke`: plumbing (the hash fixture in CI); regressions fail like `regression`.
- `regression`: a guard. `eval compare`'s tolerances decide pass or fail.
- `quality`: a capability number. Reported with its confidence interval and the smallest
  change the set can detect; it fails only on a significantly worse paired test.
- `tune`: the rows `eval sweep` may tune on (usually `split: dev`); never compared.

Sets whose manifest fixture is `any` (the portable no-memory set) run on any target; every
other set must name the suite's fixture, so a set is never scored against stores it was not
written for. Outputs hold ids and aggregates only, like every receipt.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path

from .. import __version__
from ..assets import eval_path
from .compare import compare, load_tolerances
from .format import SPLITS, EvalSet, load_set, sha256_bytes
from .run import Receipt

SCHEMA = "quoth-evalsuite/v1"
RUN_SCHEMA = "quoth-evalsuite-run/v1"
COMPARE_SCHEMA = "quoth-evalsuite-compare/v1"
ROLES = ("smoke", "regression", "quality", "tune")
ANY_FIXTURE = "any"
# Paired tests a quality set fails on when significantly worse.
QUALITY_TESTS = ("vault_hit@10", "injected_hit", "abstain_injected", "all@10")
SUITE_KEYS = {"schema", "name", "fixture", "sets", "tolerances", "description"}
ENTRY_KEYS = {"id", "path", "builtin", "role", "split", "tolerances"}


class SuiteError(ValueError):
    pass


@dataclass
class Entry:
    id: str
    role: str
    path: Path
    split: str | None
    tolerances: Path | None


@dataclass
class Suite:
    name: str
    fixture: str
    entries: list[Entry]
    tolerances: Path | None
    path: Path
    sha256: str


def _resolve(base: Path, raw: str) -> Path:
    return (base / os.path.expandvars(os.path.expanduser(raw))).resolve()


def builtin_path(name: str) -> Path:
    if not name or "/" in name or name.startswith("."):
        raise SuiteError(f"builtin set name {name!r} is not a plain name")
    return eval_path(f"sets/{name}/set.jsonl")


def load_suite(path: str | Path) -> Suite:
    """Read and check a suite file; raises SuiteError naming the first problems."""
    path = Path(path)
    try:
        raw = path.read_bytes()
        doc = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise SuiteError(f"suite unreadable: {type(exc).__name__}") from exc
    errors = []
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise SuiteError(f"suite schema must be {SCHEMA}")
    if set(doc) - SUITE_KEYS:
        errors.append(f"unknown suite keys {sorted(set(doc) - SUITE_KEYS)}")
    for key in ("name", "fixture"):
        if not isinstance(doc.get(key), str) or not doc[key].strip():
            errors.append(f"suite {key} must be a non-empty string")
    entries, ids = [], set()
    for i, e in enumerate(doc.get("sets") or []):
        tag = f"sets[{i}]"
        if not isinstance(e, dict) or set(e) - ENTRY_KEYS:
            errors.append(f"{tag} must be an object with only {sorted(ENTRY_KEYS)}")
            continue
        if not isinstance(e.get("id"), str) or not e["id"] or e["id"] in ids or "/" in e["id"]:
            errors.append(f"{tag}.id must be a unique plain name")
            continue
        ids.add(e["id"])
        if e.get("role") not in ROLES:
            errors.append(f"{e['id']}: role must be one of {list(ROLES)}")
        if ("path" in e) == ("builtin" in e):
            errors.append(f"{e['id']}: give exactly one of path or builtin")
            continue
        if e.get("split") is not None and e["split"] not in SPLITS:
            errors.append(f"{e['id']}: split must be dev or test")
        try:
            set_path = builtin_path(e["builtin"]) if "builtin" in e else _resolve(path.parent, e["path"])
        except SuiteError as exc:
            errors.append(f"{e['id']}: {exc}")
            continue
        tol = _resolve(path.parent, e["tolerances"]) if e.get("tolerances") else None
        entries.append(Entry(e["id"], e.get("role", ""), set_path, e.get("split"), tol))
    if not entries and not errors:
        errors.append("suite lists no sets")
    if errors:
        raise SuiteError("; ".join(errors[:10]))
    tol = _resolve(path.parent, doc["tolerances"]) if doc.get("tolerances") else None
    return Suite(doc["name"], doc["fixture"], entries, tol, path, sha256_bytes(raw))


def load_entry(suite: Suite, entry: Entry) -> EvalSet:
    """The entry's set, validated, checked against the suite's fixture, and selected."""
    evalset = load_set(entry.path)
    fixture = evalset.manifest.get("fixture")
    if fixture not in (ANY_FIXTURE, suite.fixture):
        raise SuiteError(f"{entry.id}: set fixture {fixture!r} is not the suite's {suite.fixture!r} or 'any'")
    return evalset.select(entry.split)


def _headline(receipt: dict) -> dict:
    metrics = receipt.get("metrics") or {}
    overall = metrics.get("overall") or {}
    vault = (overall.get("stores") or {}).get("vault") or {}
    out = {"scored": receipt.get("scored"), "vault.hit@10": vault.get("hit@10"),
           "vault.mrr": vault.get("mrr"), "injected_hit_rate": overall.get("injected_hit_rate"),
           "abstain_nonempty_rate": overall.get("abstain_nonempty_rate"), "ci95": metrics.get("ci95")}
    if overall.get("groups"):
        out["groups.all@10"] = overall["groups"].get("all@10")
    return out


def run_suite(suite: Suite, target, out_dir: str | Path, *, run_set_fn=None, **run_kw) -> dict:
    """Score every set of `suite` on `target`; writes `<id>.receipt.json` per set and
    `suite.json` into `out_dir`. Returns the summary."""
    from .run import run_set
    run_set_fn = run_set_fn or run_set
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    sets = {}
    for entry in suite.entries:
        evalset = load_entry(suite, entry)
        receipt, _ = run_set_fn(evalset, target, **run_kw)
        receipt.write(out / f"{entry.id}.receipt.json")
        sets[entry.id] = {"role": entry.role, "split": entry.split, "receipt": f"{entry.id}.receipt.json",
                          "status": receipt.status, "set_sha256": evalset.sha256,
                          "headline": _headline(receipt.data)}
    summary = {"schema": RUN_SCHEMA, "created_at": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
               "code": {"package": "quoth", "version": __version__},
               "suite": {"name": suite.name, "fixture": suite.fixture, "sha256": suite.sha256},
               "sets": sets}
    (out / "suite.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return summary


def compare_suites(a_dir: str | Path, b_dir: str | Path, suite: Suite | None = None) -> dict:
    """Compare two suite runs set by set. Exit: 0 pass, 1 a regression (or a significantly
    worse quality set), 3 not comparable (a set missing on one side, or not comparable)."""
    a_dir, b_dir = Path(a_dir), Path(b_dir)
    sa = json.loads((a_dir / "suite.json").read_text(encoding="utf-8"))
    sb = json.loads((b_dir / "suite.json").read_text(encoding="utf-8"))
    if sa.get("schema") != RUN_SCHEMA or sb.get("schema") != RUN_SCHEMA:
        raise ValueError(f"suite runs must be {RUN_SCHEMA}")
    entries = {e.id: e for e in suite.entries} if suite else {}
    base_tol = load_tolerances(suite.tolerances) if suite and suite.tolerances else None
    results, problems = {}, []
    for sid, meta in sa["sets"].items():
        role = meta["role"]
        if role == "tune":
            continue
        if sid not in sb["sets"]:
            problems.append(f"{sid}: missing in B")
            continue
        entry = entries.get(sid)
        tol = load_tolerances(entry.tolerances) if entry and entry.tolerances else base_tol
        res = compare(Receipt.load(a_dir / meta["receipt"]).data,
                      Receipt.load(b_dir / sb["sets"][sid]["receipt"]).data, tol)
        if role == "quality" and res["exit"] == 1:
            worse = [n for n in QUALITY_TESTS
                     if (res["paired"].get(n) or {}).get("significant")
                     and res["paired"][n]["direction"] == "worse"]
            # A stale value reaching the block is never noise.
            worse += [c["metric"] for c in res["checks"] if c["metric"].endswith("stale_leak")
                      and c["result"] == "fail"]
            res = {**res, "result": "fail" if worse else "pass", "exit": 1 if worse else 0,
                   "quality_failed_on": worse}
        results[sid] = {"role": role, "result": res["result"], "exit": res["exit"], "compare": res}
    codes = [r["exit"] for r in results.values()]
    exit_code = 3 if problems or 3 in codes else 1 if 1 in codes else 0
    return {"schema": COMPARE_SCHEMA, "result": {0: "pass", 1: "fail", 3: "incomparable"}[exit_code],
            "exit": exit_code, "problems": problems, "sets": results}
