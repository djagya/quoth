"""Past intent and past values: "what was X before" in recall.

Recall serves the claims that hold now. When a turn asks about the past, prefetch also
serves the values those claims replaced (`ledger.core.history`), labelled as past values
with their dates. This module holds the two pure pieces of that path:

- `past_cue(text)` / `asks_past(text)`: a small, conservative lexical detector of past
  intent in English, Russian and German. It runs on every turn, so it is a handful of
  regular expressions and nothing else. Strong cues ("used to", "previously", "back then",
  "what was", "раньше" opening a sentence, "до этого", "damals", a past year such as
  "in 2019") count alone. Weak cues ("before", "earlier", "раньше", "früher", "vorher")
  count only in a sentence that also has a past-tense marker ("did", "was", "был",
  "war", "gewohnt"): "Before I go, where do I live?" is not a past question.
- `interleave(...)`: the block order: each current claim, then its past values newest
  first, cut at the claims cap. The sidecar and `eval sweep`'s replay both use it.

Callers that know better override the detector: `/v1/prefetch` and `evidence_bundle`
take `include_history` (true forces the past path, false turns it off).
"""
from __future__ import annotations

from datetime import date
import re
import unicodedata
from typing import Callable, Hashable, Iterable, TypeVar

T = TypeVar("T")

# ---------------------------------------------------------------- past intent

_STRONG = {
    "en.previously": r"\bpreviously\b",
    "en.formerly": r"\bformerly\b",
    "en.back_then": r"\bback then\b",
    "en.in_the_past": r"\bin the past\b",
    "en.what_was": r"\bwhat (?:was|were)\b",
    "en.last_year": r"\blast year\b",
    # "I used to", "did we use to"; never "getting used to", "is used to build".
    "en.used_to": r"\b(?:i|you|we|he|she|they|it|who)\s+(?:always\s+|never\s+|still\s+|once\s+)?used to\b",
    "en.did_use_to": r"\bdid\b[^.!?\n]*\buse to\b",
    # "прежде всего" (first of all) and "прежде чем" (before doing) are not the past.
    "ru.prezhde": r"\bпрежде\b(?!\s+(?:всего|чем))",
    "ru.do_etogo": r"\bдо этого\b",
    "ru.v_proshlom": r"\bв прошлом\b",
    "ru.kogda_to": r"\bкогда-то\b",
    "ru.chto_bylo": r"\bчто (?:было|были|был|была)\b",
    "ru.ranshe_start": r"^\W*раньше\b(?!\s+чем)",
    "de.damals": r"\bdamals\b",
    "de.ehemals": r"\behemal(?:s|ige[nmrs]?)\b",
    "de.frueher_start": r"^\W*früher\b(?!\s+als)",
    "de.letztes_jahr": r"\b(?:letztes|voriges|im letzten|im vorigen) jahr\b",
    "ru.proshlom_godu": r"\bв прошлом году\b",
}
_WEAK = {
    "en.before": r"\bbefore\b",
    "en.earlier": r"\bearlier\b",
    "en.prior": r"\bprior\b",
    "ru.ranshe": r"\bраньше\b(?!\s+чем)",
    "ru.do_togo": r"\bдо того\b(?!\s*,?\s*как)",
    "de.frueher": r"\bfrüher\b(?!\s+als)",
    "de.vorher": r"\bvorher\b",
    "de.zuvor": r"\bzuvor\b",
}
# Past-tense markers that make a weak cue count (same sentence).
_PAST_MARKERS = [
    r"\b(?:was|were|did|had|used|didn't|wasn't|weren't)\b",
    r"\b(?:have|has)\s+(?:\w+\s+)?been\b",
    # Russian past tense: a vowel then -л/-ла/-ло/-ли (был, жил, работала, занимался).
    r"\b\w+[аеиоуыяё]л(?:а|о|и|ся|ась|ось|ись)?\b",
    r"\b(?:war|waren|warst|wart|hatte|hattest|hatten|hattet|gab|ging|wohnte|arbeitete)\b",
    r"\bge\w{3,}(?:t|en)\b",  # Partizip II: gewohnt, gearbeitet, gegangen
]
# Nouns that look like a Russian past verb; they never make a weak cue count.
_RU_NOT_VERBS = frozenset(("стол", "пол", "зал", "угол", "канал", "сигнал", "журнал", "материал",
                           "финал", "дела", "тело", "мало", "село", "сила", "игла", "мысли"))
_YEAR = re.compile(r"\b(?:in|back in|during|as of|в|во|за|im|im jahr|im jahre)\s+(\d{4})\b")
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_SENTENCES = re.compile(r"[^.!?;\n]+")
_STRONG_RE = [(name, re.compile(rx)) for name, rx in _STRONG.items()]
_WEAK_RE = [(name, re.compile(rx)) for name, rx in _WEAK.items()]
_MARKER_RE = [re.compile(rx) for rx in _PAST_MARKERS]


def _fold(text: str) -> str:
    return unicodedata.normalize("NFC", str(text or "")).casefold().replace("ё", "е")


def _has_past_marker(sentence: str) -> bool:
    for rx in _MARKER_RE:
        for m in rx.finditer(sentence):
            if m.group(0) not in _RU_NOT_VERBS:
                return True
    return False


def past_cue(text: str, today: date | None = None) -> str | None:
    """The name of the first past-intent cue in `text` (e.g. "en.used_to"), None when the
    turn does not ask about the past. `today` bounds the year and date cues (a year counts
    only when it is before this one; a date only when it is before today)."""
    folded = _fold(text)
    if not folded.strip():
        return None
    today = today or date.today()
    for sentence in _SENTENCES.findall(folded):
        sentence = sentence.strip()
        if not sentence:
            continue
        for name, rx in _STRONG_RE:
            if rx.search(sentence):
                return name
        for m in _YEAR.finditer(sentence):
            if 1900 <= int(m.group(1)) < today.year:
                return "year"
        for m in _ISO_DATE.finditer(sentence):
            try:
                when = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                continue
            if when < today:
                return "date"
        weak = next((name for name, rx in _WEAK_RE if rx.search(sentence)), None)
        if weak is not None and _has_past_marker(sentence):
            return weak
    return None


def asks_past(text: str, today: date | None = None) -> bool:
    """Does this turn ask what was true before? (`past_cue`)."""
    return past_cue(text, today) is not None


def wants_history(flag: bool | None, query: str, today: date | None = None) -> str | None:
    """Why a turn takes the past path: "flag" (the caller asked), "intent" (the detector),
    or None. An explicit False turns the path off whatever the text says."""
    if flag is True:
        return "flag"
    if flag is False:
        return None
    return "intent" if asks_past(query, today) else None


# ---------------------------------------------------------------- block order

def interleave(currents: Iterable[T], history: dict, cap: int, per_claim: int,
               key: Callable[[T], Hashable] = lambda c: c["id"]) -> list:
    """Each current claim followed by at most `per_claim` of its past values (`history`:
    key -> newest first), the whole cut at `cap` items. The current value always comes
    before its own past values; a past value never enters without its current claim."""
    out: list = []
    for current in currents:
        for item in [current, *list(history.get(key(current)) or [])[:max(0, per_claim)]]:
            if len(out) >= cap:
                return out
            out.append(item)
    return out


def span(item: dict) -> str:
    """"from 2026-03-01 until 2026-10-01" (ledger dates: when memory recorded the value and
    when a newer one replaced it); either end may be missing."""
    start = str(item.get("valid_from") or "")[:10]
    end = str(item.get("valid_to") or item.get("superseded_at") or "")[:10]
    parts = ([f"from {start}"] if start else []) + ([f"until {end}"] if end else [])
    return " ".join(parts) or "date unknown"
