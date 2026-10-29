"""Entity signal: cheap regex/gazetteer-free entity pass, no LLM.

Index side: capitalized words (Latin or Cyrillic) and digit-bearing tokens are
counted per chunk into the `entities` table at ingest.
Query side: the same extractor picks entity tokens out of the query; matching
chunks form a third RRF list in `search`.
"""

from __future__ import annotations

import re

MIN_LEN = 4

_TOKEN = re.compile(r"[^\W_]+(?:[-'][^\W_]+)*", re.UNICODE)

# Capitalized words that start a question and are not entities.
_QUERY_START = {
    "What", "Why", "How", "Who", "Where", "When", "Which", "Whose",
    "Was", "Were", "Did", "Does", "Is", "Are", "On", "In", "At", "The",
    "Сколько", "Почему", "Что", "Как", "Кто", "Где", "Когда", "Какой", "Какая",
    "Какое", "Какие", "Который", "Был", "Была", "Было",
}

# Capitalized words that appear mid-sentence but are not entities.
_CAP_STOP = set(_QUERY_START) | {
    "This", "That", "Then", "There", "These", "Those", "They", "Their",
    "Также", "Итак", "Только", "Если", "Чтобы", "Когда", "Если",
}


def _norm(w: str) -> str:
    return w.lower().replace("ё", "е")


def _is_entity_token(w: str) -> bool:
    if len(w) < MIN_LEN:
        return False
    if w[0].isdigit():
        return True  # years, dates, codes: 2026, 2026-05-22, 2877
    if not w[0].isupper():
        return False
    return w not in _CAP_STOP


def extract_entities(text: str) -> dict[str, int]:
    """Count entity tokens in `text` (normalized: lowercase, ё→е)."""
    counts: dict[str, int] = {}
    for m in _TOKEN.finditer(text):
        w = m.group(0)
        if _is_entity_token(w):
            n = _norm(w)
            counts[n] = counts.get(n, 0) + 1
    return counts


def query_entities(query: str) -> list[str]:
    """Entity tokens of a query; a capitalized first word of a question is skipped."""
    toks = [m.group(0) for m in _TOKEN.finditer(query)]
    out = []
    for i, w in enumerate(toks):
        if i == 0 and w in _QUERY_START:
            continue
        if _is_entity_token(w):
            n = _norm(w)
            if n not in out:
                out.append(n)
    return out


def entities_for_chunk(title: str, heading_path: str, text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for source in (title, heading_path, text):
        for k, v in extract_entities(source).items():
            counts[k] = counts.get(k, 0) + v
    return counts
