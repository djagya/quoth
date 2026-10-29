"""Compile accepted core claims into a budgeted, always-loaded core block artifact.

The output is an immutable file `core-<sha12>.md` plus `core-<sha12>.json`, published
by atomically replacing the `core.md`/`core.json` symlinks. Hermes reads `core.md`.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import uuid

from .core import AUTHORITY, _CURRENT, _now, _tx

# Section order of the core block; episodes stay in recall, never in the core.
CORE_KINDS = ("identity", "consent", "correction", "preference", "environment", "person",
              "taste", "project_state", "pointer")
TITLES = {"identity": "Identity", "consent": "Consent and boundaries", "correction": "Corrections",
          "preference": "Preferences", "environment": "Environment", "person": "People",
          "taste": "Taste", "project_state": "Projects", "pointer": "Where things are"}
HEADER = ("Core memory (accepted by the owner or stated in the owner's own words). Entries"
          " marked [inferred] are your inferences that the owner reviewed, not the owner's"
          " words.")


def compile_core(conn, budget_chars: int = 4000, headroom: float = 0.2) -> dict:
    """Return {text, claim_ids, dropped_ids, chars, limit, budget}; fills to budget*(1-headroom)."""
    if budget_chars <= 0 or not 0 <= headroom < 1:
        raise ValueError("invalid core budget")
    limit = int(budget_chars * (1 - headroom))
    rows = conn.execute(
        f"SELECT * FROM claims WHERE review_status = 'accepted' AND {_CURRENT}"
        f" AND kind IN ({','.join('?' * len(CORE_KINDS))})", CORE_KINDS).fetchall()
    order = {kind: i for i, kind in enumerate(CORE_KINDS)}
    # Within a section: most used first, then oldest first (stable, deterministic).
    rows = sorted(rows, key=lambda r: (order[r["kind"]], -r["use_count"], r["id"]))
    chosen: dict[str, list] = {}
    dropped = []
    length = len(HEADER)
    for row in rows:
        line = "- " + row["text"].replace("\n", " ").strip()
        if AUTHORITY[row["origin"]] == "assistant":
            line += " [inferred]"
        cost = len(line) + 1 + (0 if row["kind"] in chosen else len(TITLES[row["kind"]]) + 5)
        if length + cost > limit:
            dropped.append(row["id"])
            continue
        chosen.setdefault(row["kind"], []).append((row["id"], line))
        length += cost
    parts = [HEADER]
    ids = []
    for kind in CORE_KINDS:
        if kind in chosen:
            parts.append(f"\n## {TITLES[kind]}")
            for claim_id, line in chosen[kind]:
                parts.append(line)
                ids.append(claim_id)
    text = "\n".join(parts) + "\n"
    return {"text": text, "claim_ids": ids, "dropped_ids": dropped, "chars": len(text),
            "limit": limit, "budget": budget_chars}


def published_core(core_dir: str | Path | None) -> dict | None:
    """The published core block (`core.md` in `core_dir`), or None when nothing is published.

    The sidecar serves this to Hermes and the WebUI shows the same file, so both agree.
    """
    if core_dir is None or not (Path(core_dir) / "core.md").exists():
        return None
    target = (Path(core_dir) / "core.md").resolve()
    return {"source": "published", "file": target.name, "text": target.read_text(encoding="utf-8")}


def _symlink(target: Path, link: Path) -> None:
    temporary = link.with_name("." + link.name + "." + uuid.uuid4().hex)
    try:
        temporary.symlink_to(target.name)
        os.replace(temporary, link)
    finally:
        temporary.unlink(missing_ok=True)
    fd = os.open(link.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def publish_core(conn, out_dir: str | Path, budget_chars: int = 4000, headroom: float = 0.2) -> dict:
    """Write the compiled core as an immutable artifact and repoint core.md/core.json."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    compiled = compile_core(conn, budget_chars, headroom)
    data = compiled["text"].encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    stem = "core-" + digest[:12]
    manifest = {"schema": "quoth-core/1", "sha256": digest, "file": stem + ".md",
                "claim_ids": compiled["claim_ids"], "dropped_ids": compiled["dropped_ids"],
                "chars": compiled["chars"], "limit": compiled["limit"], "budget": budget_chars,
                "compiled_at": _now()}
    md, js = out / (stem + ".md"), out / (stem + ".json")
    for path, payload in ((md, data), (js, (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())):
        if path.exists():
            if path == md and path.read_bytes() != payload:
                raise ValueError("core artifact digest collision")
            continue
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    _symlink(md, out / "core.md")
    _symlink(js, out / "core.json")
    with _tx(conn):
        conn.execute("INSERT OR IGNORE INTO core_versions(sha256, content, claim_ids, budget)"
                     " VALUES (?,?,?,?)", (digest, compiled["text"],
                                           json.dumps(compiled["claim_ids"]), budget_chars))
    return {**manifest, "path": str(out / "core.md")}
