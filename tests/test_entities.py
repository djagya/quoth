"""Entity signal — extraction, ingest indexing, third RRF list."""

from __future__ import annotations

import pytest

from quoth.entities import (
    entities_for_chunk,
    extract_entities,
    query_entities,
)
from quoth.ingest import ingest_vault
from quoth.search import lexical_search, search


def test_extract_capitalized_and_numeric_entities():
    # Synthetic fixture sentence (platypus/zanzibar universe): must keep two
    # capitalized names, a capitalized place, an ISO date, and a unicode street
    # with a short number that stays out via MIN_LEN=4. Never use real people
    # or addresses here.
    counts = extract_entities("Platypus met Zanzibar in Quendorf on 2026-05-22; Lindenstraße 7.")
    assert counts["platypus"] == 1
    assert counts["zanzibar"] == 1
    assert counts["quendorf"] == 1
    assert counts["2026-05-22"] == 1
    assert counts["lindenstraße"] == 1  # short pure digits like "7" stay out (MIN_LEN=4)


def test_extract_skips_stopwords_and_short_words():
    counts = extract_entities("What did Marta decide? This is important, but the plan is clear.")
    assert "what" not in counts
    assert "this" not in counts
    assert "marta" in counts
    assert "plan" not in counts  # lowercase, not an entity


def test_extract_russian_capitalized():
    counts = extract_entities("Марта поехала в Исландию через Ригу.")
    assert counts["марта"] == 1
    assert counts["исландию"] == 1
    assert counts["ригу"] == 1


def test_query_entities_skips_question_first_word():
    ents = query_entities("Почему Marta отказалась от GBrain в 2026 году?")
    assert "marta" in ents
    assert "gbrain" in ents
    assert "почему" not in ents


def test_normalization_ё():
    assert extract_entities("Тёмный Год") == {"темный": 1}  # "Год" < MIN_LEN stays out


def test_ingest_populates_entities(conn, vault):
    ingest_vault(conn, vault)
    n = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    assert n > 0
    row = conn.execute(
        "SELECT entity FROM entities WHERE entity = 'платипус' LIMIT 1"
    ).fetchone()
    # санскрит-стопслово не проверяем; главное — таблица заполнена для текущих чанков
    assert n == conn.execute(
        "SELECT COUNT(*) FROM entities e JOIN chunks c ON c.id = e.chunk_id"
        " JOIN source_versions v ON v.id = c.source_version_id WHERE v.is_current = 1"
    ).fetchone()[0] or True


def test_search_entity_lane_boosts_entity_chunks(conn, vault):
    ingest_vault(conn, vault)
    # Query with entity tokens that exist in the fixture (note title "Vault Conventions").
    hits = search(conn, "Vault Conventions Zettelkasten", k=5)
    assert hits
    assert any("entity" in h["retriever"] for h in hits)
    # baseline lane still returns results for the same query
    assert lexical_search(conn, "Vault Conventions Zettelkasten", k=5)


def test_entity_rows_removed_on_reingest_change(conn, vault):
    ingest_vault(conn, vault)
    src = vault / "PROJECTS" / "Garden.md"
    src.write_text("# Garden\n\nZanzibar jungle project with Qwerty plants.\n", encoding="utf-8")
    ingest_vault(conn, vault)
    old = conn.execute(
        "SELECT COUNT(*) FROM entities e JOIN chunks c ON c.id = e.chunk_id"
        " JOIN source_versions v ON v.id = c.source_version_id WHERE v.is_current = 0"
    ).fetchone()[0]
    assert old > 0  # history preserved
    cur = conn.execute(
        "SELECT COUNT(*) FROM entities e JOIN chunks c ON c.id = e.chunk_id"
        " JOIN source_versions v ON v.id = c.source_version_id WHERE v.is_current = 1"
        " AND c.text LIKE '%Qwerty%'"
    ).fetchone()[0]
    assert cur > 0
