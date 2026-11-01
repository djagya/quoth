"""Relevance gate and lexical measure for prefetch injection.

Retrieval and candidate lists are unchanged. The gate decides which of a turn's candidates
(its vault pool, its claim pool) reach the recall block, so a turn with nothing relevant
injects nothing. It reads signals already at hand when evidence returns, no extra model or
index call:

- ``cosine``: the dense similarity the store ranked by (bge-m3, normalised vectors);
- ``gap``: how far a vault hit's cosine sits below the turn's top vault hit;
- ``lexical``: the share of the query's content words found in the item's text, and
  ``matched``, how many (a lexical rescue needs at least RESCUE_MIN_TERMS);
- ``rerank`` (0.13.2, only with a reranker loaded, `quoth.rerank`): a cross-encoder's
  sigmoid score of (query, item text). Where a turn has it for every hit it would rerank,
  the rerank thresholds decide those hits instead of the cosine ones; where it is missing
  (model loading or unavailable, over its time budget) the cosine gate applies.

A vault hit's lexical coverage is taken on `vault_text`, the same whitespace-folded,
VAULT_SNIPPET-character excerpt the recall block shows. The reranker reads more
(`rerank_text`): the note title and heading path, as the embedder does, then the chunk text
up to the reranker's passage bound. Scored on the bare excerpt it ranked below cosine order;
with the context it ranks above it. A turn judges every candidate in its pool and injects the best that pass
(`vault_pick`), so a dropped slot is refilled by the next candidate.

`Gate()` has every threshold off (every item passes with reason ``off``); `default_gate()`
holds the serving defaults (`recall_defaults.GATE`), which the sidecar and `eval run` start
from. `quoth eval sweep` replays the same `Gate` on recorded signals, so a threshold chosen
offline behaves the same on the sidecar.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from pathlib import PurePosixPath
import re
import unicodedata

from . import recall_defaults as D

_WORD = re.compile(r"\w+", re.UNICODE)
# Words compare by a light stem: one inflection ending stripped, then the first STEM letters.
STEM = 6
# Cyrillic endings (longest first); a stem keeps at least 2 letters.
_CYR_ENDINGS = ("ями", "ами", "ого", "его", "ому", "ему", "ыми", "ими", "ах", "ях", "ам", "ям", "ом",
                "ем", "ой", "ей", "ий", "ый", "ая", "ое", "ую", "юю", "ов", "ев",
                "а", "я", "у", "ю", "е", "и", "ы", "о", "й", "ь")
_CYRILLIC = re.compile(r"[а-я]")
# A lexical rescue needs this many matched words: one shared name is not relevance.
RESCUE_MIN_TERMS = 2
# Function words of the set's languages (en, ru, pt, de) and conversational filler (the
# assistant's name, thanks, greetings); shorter words are dropped anyway.
STOPWORDS = frozenset(w.replace("ё", "е") for w in """
the and for are was were what when where which who whom whose why how that this these those
with from have has had does did done about into onto your yours you our ours his her hers their
theirs they them there then than but not can could would should will shall all any some its
just also very too more most much many such only own same other again once here out off over
under above below between after before during while because until been being
что как где когда это этот эта эти тот там тут для или его она они оно был была было были есть
мне меня мой моя моё мои так уже все всё ещё еще про при над под без чем кто нас вас вам нам
тебя тебе себя свой своя свои какой какая какое какие сколько почему зачем если чтобы только
очень тоже также надо нужно можно будет было
который которая которое которые которого которой котором которую которых которым
этого этому этим этой этом эту того тому тем том тех теми весь вся всего всей всем всех
наш наша наше наши нашего нашей ваш ваша ваше ваши мной тобой ему ней нее неё них ним ними
чего чему сейчас потом теперь тогда даже вот вообще ведь быть буду будешь будем будут
такой такая такое такие такого таким нет либо куда откуда сам сама сами самый хочу хочет может могу
que para com uma uns umas como onde quando qual quais por pelo pela pelos pelas sobre mas não
isso isto este esta esse essa aquele aquela meu minha meus minhas você vocês ele ela eles elas
foi ser são tem têm está estão seu sua seus suas dos das nos nas num numa
und der die das ist ein eine einen einem einer mit für von wie was wer wann nicht auch auf aus
bei ich mein meine sind war hat haben dem den des sich wir ihr sie zum zur noch nur oder aber
thanks thank please hello hey спасибо пожалуйста привет obrigado obrigada olá danke bitte hallo
""".split())


def stem(word: str) -> str:
    """Light folding of a casefolded word: Москве/Москва, чая/чай, cities/city, rents/rent."""
    if _CYRILLIC.search(word):
        for ending in _CYR_ENDINGS:
            if word.endswith(ending) and len(word) - len(ending) >= 2:
                word = word[:-len(ending)]
                break
    elif word.endswith("ies") and len(word) > 4:
        word = word[:-3] + "y"
    elif word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        word = word[:-1]
    return word[:STEM]


def terms(text: str) -> frozenset[str]:
    """Content words of `text`: casefolded (ё as е), function words and 1-2 letter words
    dropped, other words stemmed (`stem`); numbers kept whole (2+ digits)."""
    out = set()
    folded = unicodedata.normalize("NFC", str(text or "")).casefold().replace("ё", "е")
    for word in _WORD.findall(folded):
        if word in STOPWORDS:
            continue
        if word.isdigit():
            if len(word) >= 2:
                out.add(word)
        elif len(word) >= 3:
            out.add(stem(word))
    return frozenset(out)


def overlap(query_terms: frozenset[str], text: str) -> tuple[float | None, int]:
    """(share of the query's content words present in `text`, None when the query has
    none; how many are present)."""
    if not query_terms:
        return None, 0
    found = len(query_terms & terms(text))
    return round(found / len(query_terms), 4), found


def coverage(query_terms: frozenset[str], text: str) -> float | None:
    return overlap(query_terms, text)[0]


def vault_text(hit: dict) -> str:
    """The excerpt of a vault hit the recall block injects, and the text the gate and the
    reranker judge: whitespace folded, cut to VAULT_SNIPPET characters."""
    return " ".join(str(hit.get("text") or "").split())[:D.VAULT_SNIPPET]


def rerank_text(hit: dict) -> str:
    """What the cross-encoder reads of a vault hit: `title — heading path`, then the chunk
    text (no aliases, which pull every chunk of a note toward its topic). `rerank.passage`
    folds whitespace and bounds it."""
    title = PurePosixPath(str(hit.get("path") or "")).stem
    heading = str(hit.get("heading_path") or "")
    head = f"{title} — {heading}" if heading and heading != title else title
    return f"{head}\n{hit.get('text') or ''}" if head else str(hit.get("text") or "")


@dataclass(frozen=True)
class Gate:
    """Thresholds per store; None leaves that check off.

    Vault: a hit passes when its lexical coverage reaches ``vault_lexical_rescue`` with at
    least RESCUE_MIN_TERMS words, else when its cosine is at least ``vault_min_cosine``
    and at most ``vault_max_gap`` below the turn's top hit. A rescue without a floor or a
    gap would gate nothing and is refused. Claims: a dense match passes at
    ``claims_min_cosine``, a word-only match at ``claims_min_lexical`` coverage; either
    path also passes on the other's bar. An item without a score is never dropped.
    """

    vault_min_cosine: float | None = None
    vault_max_gap: float | None = None
    vault_lexical_rescue: float | None = None
    claims_min_cosine: float | None = None
    claims_min_lexical: float | None = None
    # Rerank thresholds (0.13.2). Left out of `to_dict` while None, so a gate without
    # them reports exactly what 0.12.5 did (health, receipts).
    vault_min_rerank: float | None = None
    vault_max_rerank_gap: float | None = None
    vault_rerank_widen: float | None = None
    claims_min_rerank: float | None = None

    def __post_init__(self):
        if self.vault_lexical_rescue is not None and not self.vault_on:
            raise ValueError("vault_lexical_rescue needs vault_min_cosine or vault_max_gap")
        widen = self.vault_rerank_widen
        if widen is not None and (widen != int(widen) or not 1 <= widen <= MAX_WIDEN):
            raise ValueError(f"vault_rerank_widen must be a whole number of hits, 1..{MAX_WIDEN}")

    @property
    def vault_on(self) -> bool:
        return self.vault_min_cosine is not None or self.vault_max_gap is not None

    @property
    def rerank_on(self) -> bool:
        """The vault hits are judged (and with a widen, chosen) by rerank score when scored."""
        return (self.vault_min_rerank is not None or self.vault_max_rerank_gap is not None
                or self.vault_rerank_widen is not None)

    @property
    def rerank_any(self) -> bool:
        """Any rerank threshold, vault or claims (they need a loaded reranker)."""
        return self.rerank_on or self.claims_min_rerank is not None

    def rerank_depth(self, k: int) -> int:
        """How many vault hits (cosine order) a turn reranks: the served k, or the widen."""
        return max(k, int(self.vault_rerank_widen or 0))

    @property
    def claims_on(self) -> bool:
        return self.claims_min_cosine is not None or self.claims_min_lexical is not None

    def vault(self, cosine: float | None, top: float | None, lexical: float | None,
              matched: int = 0, rerank: float | None = None, rerank_top: float | None = None,
              named: bool = False) -> tuple[bool, str]:
        """(passes, reason) for one vault hit; `top` is the turn's best vault cosine,
        `matched` how many query words the hit holds; `rerank` its rerank score when the
        turn's hits are scored (None: the cosine rules apply), `rerank_top` the best one;
        `named`: the query names the hit's note exactly (`retrieval.named_paths`), which
        passes the cosine floor and gap (reason ``name``). Hybrid retrieval brings in
        lexical-only hits; below the floor this is the only way one reaches the block."""
        if self.rerank_on and rerank is not None:
            if self.vault_min_rerank is not None and rerank < self.vault_min_rerank:
                return False, "rerank_floor"
            gap = self.vault_max_rerank_gap
            if gap is not None and rerank_top is not None and round(rerank_top - rerank, 4) > gap:
                return False, "rerank_gap"
            return True, "rerank"
        if not self.vault_on:
            return True, "off"
        if cosine is None:
            return True, "unscored"
        if named:
            return True, "name"
        rescue = self.vault_lexical_rescue
        if rescue is not None and lexical is not None and lexical >= rescue and matched >= RESCUE_MIN_TERMS:
            return True, "lexical"
        if self.vault_min_cosine is not None and cosine < self.vault_min_cosine:
            return False, "below_floor"
        if self.vault_max_gap is not None and top is not None and round(top - cosine, 4) > self.vault_max_gap:
            return False, "gap"
        return True, "cosine"

    def claim(self, cosine: float | None, lexical: float | None, rerank: float | None = None) -> tuple[bool, str]:
        """(passes, reason) for one candidate claim; `cosine` is None for a word-only match,
        `rerank` its rerank score when scored (then it alone decides, at `claims_min_rerank`)."""
        if self.claims_min_rerank is not None and rerank is not None:
            return (True, "rerank") if rerank >= self.claims_min_rerank else (False, "rerank_floor")
        if not self.claims_on:
            return True, "off"
        words = self.claims_min_lexical is not None and lexical is not None and lexical >= self.claims_min_lexical
        if cosine is not None:
            if self.claims_min_cosine is None or cosine >= self.claims_min_cosine:
                return True, "cosine"
            return (True, "lexical") if words else (False, "below_floor")
        if self.claims_min_lexical is None:
            return True, "unscored"
        return (True, "lexical") if words else (False, "weak_words")

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None or k not in RERANK_FIELDS}

    def status(self) -> dict:
        """Thresholds plus whether each store is actually gated (`/v1/health`)."""
        return {**self.to_dict(), "vault_on": self.vault_on, "claims_on": self.claims_on}

    @classmethod
    def from_dict(cls, data: dict | None) -> "Gate":
        data = data or {}
        return cls(**{f.name: (None if data.get(f.name) is None else float(data[f.name]))
                      for f in fields(cls)})

    def flags(self) -> list[str]:
        """The sidecar (and `eval run`) flags that set exactly this gate: every threshold
        that differs from the serving default (`recall_defaults.GATE`), `off` for a default
        check this gate leaves off."""
        out = []
        for f in fields(self):
            value = getattr(self, f.name)
            if value != D.GATE[f.name]:
                out += ["--" + f.name.replace("_", "-"), "off" if value is None else f"{value:g}"]
        return out

    def label(self) -> str:
        """The thresholds this gate sets, `name=value` (eval tables); "no gate" when none."""
        return " ".join(f"{f.name.replace('_', '-')}={getattr(self, f.name):g}" for f in fields(self)
                        if getattr(self, f.name) is not None) or "no gate"


RERANK_FIELDS = ("vault_min_rerank", "vault_max_rerank_gap", "vault_rerank_widen", "claims_min_rerank")
MAX_WIDEN = D.MAX_WIDEN


def default_gate() -> Gate:
    """The serving defaults (`recall_defaults.GATE`): what `quoth sidecar` and `eval run` gate with."""
    return Gate(**D.GATE)


def _threshold(kind):
    """argparse type: a number, or `off` (also `none`) for that check off."""
    def parse(raw: str):
        if raw.strip().lower() in ("off", "none"):
            return None
        return kind(raw)
    parse.__name__ = kind.__name__
    return parse


def _default(name: str) -> str:
    value = D.GATE[name]
    return "off" if value is None else f"{value:g}"


def add_arguments(parser) -> None:
    """The gate's flags, shared by `quoth sidecar` and `quoth eval run`. Each defaults to
    `recall_defaults.GATE`; `off` turns that check off."""
    rules = (
        ("vault_min_cosine", float, "inject a vault hit only at or above this cosine"),
        ("vault_max_gap", float, "drop vault hits more than this cosine below the turn's top hit"),
        ("vault_lexical_rescue", float, "a vault hit holding this share of the query's words (2 or more)"
                                        " passes the floor and gap anyway; needs one of them"),
        ("claims_min_cosine", float, "inject a meaning-matched claim only at or above this cosine"),
        ("claims_min_lexical", float, "inject a word-matched claim only at this share of the query's words"),
        ("vault_min_rerank", float, "inject a vault hit only at or above this rerank score (needs"
                                    " --rerank-model; replaces the cosine rules on turns whose hits are scored)"),
        ("vault_max_rerank_gap", float, "drop vault hits more than this rerank score below the turn's best"),
        ("vault_rerank_widen", int, f"rerank the top N vault hits by cosine and serve the best"
                                    f" {D.PREFETCH_EVIDENCE} by rerank score (off: rerank the"
                                    f" {D.PREFETCH_EVIDENCE} served hits, keep cosine order)"),
        ("claims_min_rerank", float, "inject a meaning-matched claim only at or above this rerank score"),
    )
    for name, kind, text in rules:
        parser.add_argument("--" + name.replace("_", "-"), type=_threshold(kind), default=D.GATE[name],
                            metavar="X|off", help=f"relevance gate: {text} (default {_default(name)})")


def from_args(args, parser=None) -> Gate:
    """The gate the flags set; an incoherent combination is a usage error (`parser.error`)."""
    try:
        return Gate(**{f.name: getattr(args, f.name, None) for f in fields(Gate)})
    except ValueError as exc:
        if parser is None:
            raise
        parser.error(f"relevance gate: {exc}")


def judge_vault(gate: Gate, query_terms: frozenset[str], hits: list[dict], k: int = 3,
                rerank: list | None = None) -> list[dict]:
    """Per hit (ranked list): {lexical, matched, passed, gate}; `cosine` is read from the hit.
    `k` is how many hits serving injects. With `rerank` (scores aligned to `hits`, None
    where unscored: a reranker is loaded) each entry also carries its `rerank` score."""
    out = []
    for h in hits:
        lexical, matched = overlap(query_terms, vault_text(h))
        out.append({"lexical": lexical, "matched": matched})
    signals = [{"cosine": h.get("cosine"), "lexical": j["lexical"], "matched": j["matched"],
                "named": bool(h.get("named"))} for h, j in zip(hits, out)]
    for j, (passed, reason) in zip(out, decide_vault(gate, signals, k, rerank)):
        j.update(passed=passed, gate=reason)
    if rerank is not None:
        for i, j in enumerate(out):
            j["rerank"] = rerank[i] if i < len(rerank) else None
    return out


def rerank_usable(gate: Gate, rerank: list | None, n: int, k: int) -> bool:
    """Rerank rules apply on a turn only when every hit it would rerank has a score."""
    if not gate.rerank_on or rerank is None:
        return False
    depth = min(gate.rerank_depth(k), n)
    return depth > 0 and len(rerank) >= depth and all(r is not None for r in rerank[:depth])


def decide_vault(gate: Gate, items: list[dict], k: int, rerank: list | None = None) -> list[tuple[bool, str]]:
    """(passes, reason) per vault hit, in cosine order; `items` carry cosine, lexical and
    matched. The sidecar and `eval sweep` both decide through here."""
    cosines = [it["cosine"] for it in items if it["cosine"] is not None]
    top = max(cosines) if cosines else None
    scores = rerank if rerank_usable(gate, rerank, len(items), k) else None
    rerank_top = max(scores[:min(gate.rerank_depth(k), len(items))]) if scores else None
    return [gate.vault(it["cosine"], top, it.get("lexical"), it.get("matched") or 0,
                       scores[i] if scores and i < len(scores) else None, rerank_top,
                       bool(it.get("named")))
            for i, it in enumerate(items)]


def vault_pick(gate: Gate, passed: list[bool], pool: int, k: int, rerank: list | None = None,
               notes: list | None = None, per_note: int | None = D.MAX_HITS_PER_NOTE["prefetch"]) -> list[int]:
    """Indices of the vault hits a turn injects, in block order: the first `k` that pass
    among the turn's candidates (gate before the cut). The candidates are the `pool` best in
    retrieval order; when rerank rules apply (every hit the turn reranks is scored) only the
    reranked hits, in retrieval order, or with a widen by rerank score, since an unscored hit
    was never judged by the rules that decide this turn. With `notes` (each hit's note key)
    at most `per_note` passing hits of one note come before any repeat, and a repeat only
    fills a slot no other note can."""
    n = len(passed)
    if rerank_usable(gate, rerank, n, k):
        depth = min(gate.rerank_depth(k), n)
        order = (sorted(range(depth), key=lambda i: (-rerank[i], i)) if gate.vault_rerank_widen is not None
                 else list(range(depth)))
    else:
        order = list(range(min(pool, n)))
    picked = [i for i in order if passed[i]]
    if notes is None or per_note is None:
        return picked[:k]
    counts: dict = {}
    first, repeats = [], []
    for i in picked:
        if notes[i] is None:  # an unknown note counts as its own
            first.append(i)
            continue
        counts[notes[i]] = counts.get(notes[i], 0) + 1
        (first if counts[notes[i]] <= per_note else repeats).append(i)
    return (first + repeats)[:k]


def judge_claim(gate: Gate, query_terms: frozenset[str], claim: dict, cosine: float | None,
                rerank: float | None = None, scored: bool = False) -> dict:
    """`cosine` is the claim's score in the turn's dense pool (None: a word-only match),
    kept as `gate_score` so a replay judges the same input whatever the candidate depth.
    `scored` (claims are reranked): the entry also carries `rerank` (None: not scored)."""
    lexical = coverage(query_terms, claim.get("text") or "")
    passed, reason = gate.claim(cosine, lexical, rerank)
    out = {"lexical": lexical, "gate_score": cosine, "passed": passed, "gate": reason}
    if scored:
        out["rerank"] = rerank
    return out
