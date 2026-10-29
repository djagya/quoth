from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from quoth.db import open_db

NOTES = {
    "Vault Conventions.md": """\
        ---
        title: Vault Conventions
        tags: [meta, conventions]
        created: 2026-04-21
        updated: 2026-09-12
        status: active
        ---

        # Vault Conventions

        How this vault is organized. Zettelkasten principles apply.

        ## Folders

        Top-level folders are uppercase.
        """,
    "KNOWLEDGE/Медитация.md": """\
        ---
        tags:
          - практика
          - осознанность
        ---
        # Медитация

        Ежедневная медитация помогает сосредоточиться. Я медитирую по утрам.

        ## Техники

        Дыхательные практики и сканирование тела.
        """,
    "WORK/Quarterly plan.md": """\
        # Quarterly plan

        Ship the ingestion pipeline and the retrieval benchmark.
        """,
    "HEALTH/Bloodwork.md": "# Bloodwork\n\nFerritin was low; the platypus protocol applies.\n",
    "JOURNAL/Session 1.md": "# Session\n\nTalked about the platypus dream.\n",
    "PRIVATE/notes.md": "Private platypus.\n",
    "ME/Diary.md": "Platypus entry.\n",
    "ME/dreams/2026-01-01.md": "Dreamt of a platypus.\n",
    "ME/morning pages/day1.md": "Platypus morning page.\n",
    "ME/Boundaries.md": "Platypus boundaries.\n",
    "ME/Profile.md": "# Profile\n\nPublic platypus fan.\n",
    "PROJECTS/Garden.md": "# Garden\n\nTomatoes and basil grow well in the south bed.\n",
    ".obsidian/workspace.md": "should never be indexed zanzibar\n",
    ".trash/old.md": "trashed zanzibar\n",
    ".git/x.md": "git zanzibar\n",
    "PROJECTS/attachments/a.md": "attachment zanzibar\n",
    "PROJECTS/image.png": "not markdown zanzibar\n",
}


def write_vault(root: Path, notes: dict[str, str] = NOTES) -> Path:
    for rel, body in notes.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(body), encoding="utf-8")
    return root


def gate_set(path: Path, rows: list[dict]) -> Path:
    """Write golden-v1 rows (adding ``lang``) and their eval set v1 conversion.

    Returns the converted set (``<path stem>.evalset.jsonl`` next to ``path``), the file the
    nightly gate's ``--gate-set`` takes; ``path`` keeps the golden-v1 rows for ``--golden``.
    """
    from quoth.evals import convert
    import json
    path.write_text("".join(json.dumps({"lang": "en", **r}) + "\n" for r in rows), encoding="utf-8")
    out = path.with_name(path.stem + ".evalset.jsonl")
    convert("golden-v1", path, out, privacy="public", fixture="tests")
    return out


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    return write_vault(tmp_path / "vault")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "memory.db"


@pytest.fixture
def conn(db_path):
    c = open_db(db_path)
    yield c
    c.close()
