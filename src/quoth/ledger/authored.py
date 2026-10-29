"""Owner-authored text extraction from Hermes state.db rows, and quote verification.

The extraction rules (constants and the functions from `_clean_text` through
`_authored_payload`) follow Hermes' transport formats: what the gateway wraps around a
message (reply quotes, voice transcripts, attachment captions, runtime notes, the
`[<sender>] ` tag of shared sessions) is removed, and only what the owner typed or
dictated counts as the owner's own words. Unknown wrappers fail closed.
"""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from pathlib import Path

ALLOWED_SOURCES = {"telegram"}
SYNTHETIC_PREFIXES = (
    "[CONTEXT COMPACTION — REFERENCE ONLY]",
    "[Your active task list was preserved across context compression]",
    "[ASYNC DELEGATION",
    '[IMPORTANT: The user has invoked the "',
    "[IMPORTANT: MCP servers have been reloaded",
    "[IMPORTANT: Background process ",
    "[Telegram reaction update:",
    "[SYSTEM:",
    "[System note:",
    "[Note: model was just switched",
    # Hermes-injected "user" turns (kanban wakes, cron deliveries, goal nudges, process and
    # completion notices, compaction carriers). Formats from hermes-agent (gateway/wake.py,
    # cron/scheduler_delivery.py, hermes_cli/goals.py, tools/process_registry_notifications.py,
    # agent/context_compressor.py).
    "[kanban",
    "[Cron delivery:",
    "[Cronjob",
    "Cronjob Response:",
    "[Continuing toward",
    "[IMPORTANT:",
    "[ASYNC ",
    "[PRIOR CONTEXT",
    "[CONTEXT SUMMARY",
    "[Planning state preserved",
    "[OUT-OF-BAND USER MESSAGE",  # wrapper format not parsed: fail closed
    "[Telegram ",
    "A background subagent you dispatched earlier has finished.",
    "A background fan-out of ",
    "[Your ",
)
# Hermes display kinds of rows the gateway injects into a chat as role='user'.
INJECTED_DISPLAY_KINDS = ("internal_notification", "hidden", "async_delegation_complete")
STRIPPABLE_RUNTIME_NOTE_PREFIXES = (
    "[System note:",
    "[Note: model was just switched",
)

_WS_RE = re.compile(
    r"[ \t\r\f\v\x1e\x1f\x85\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]+"
)

# The sender tag Hermes puts before a message in a shared session: `[<display name>] `.
# A bracketed name without a colon; Hermes' own bracketed markers are never a tag.
SENDER_TAG = (r"\[(?!(?:image attachment|non-text attachment|Image attached at|Replied-to image"
              r"|The user sent|Content of|Replying to)\b)[^\[\]\n:]{1,64}\]")
_LEADING_TAG_RE = re.compile(r"^" + SENDER_TAG + r"\s*")
_TRAILING_TAG_RE = re.compile(r"\n+" + SENDER_TAG + r"\s*$")

MIN_QUOTE_CHARS = 8
MAX_QUOTE_CHARS = 500
LOCATOR_RE = re.compile(r"^hermes:(?P<session>[A-Za-z0-9_.:-]{1,128}):(?P<message>[0-9]{1,18})$")


def _clean_text(text: str | None) -> str:
    # SQLite may return bytes when a row was stored as BLOB. A binary payload is
    # never owner-authored text; treat it as empty so the row is excluded as
    # non-text rather than crashing the manifest build.
    if isinstance(text, bytes):
        return ""
    return (text or "").strip()


def _strip_leading_runtime_notes(text: str) -> str:
    """Remove generated prefaces while preserving an appended real user payload."""
    raw = text.strip()
    while any(raw.startswith(prefix) for prefix in STRIPPABLE_RUNTIME_NOTE_PREFIXES):
        boundary = raw.find("]\n\n")
        if boundary < 0:
            return ""
        raw = raw[boundary + 3 :].strip()
    return raw


# A runtime note may also be appended after the authored payload (session-reset
# and gateway-restart notes). Only a trailing standalone note is stripped:
# mid-message mentions of these markers are user content. The note may follow
# transport markers separated by a single newline.
TRAILING_RUNTIME_NOTE_RE = re.compile(
    r"\n+\[(?:System note|Note: model was just switched)[^\]]*\]\s*$"
)


def _strip_trailing_runtime_notes(text: str) -> str:
    while True:
        stripped = TRAILING_RUNTIME_NOTE_RE.sub("", text)
        if stripped == text:
            return stripped
        text = stripped


def _synthetic_reason(text: str, source: str) -> str | None:
    if source not in ALLOWED_SOURCES:
        return f"excluded_source:{source}"
    text = _strip_leading_runtime_notes(text)
    if not text or re.fullmatch(r"[\s\u200b]*", text):
        return "empty"
    # A leading transport sender tag does not make an injected payload authored.
    bare = _LEADING_TAG_RE.sub("", text, count=1)
    if any(text.startswith(prefix) or bare.startswith(prefix) for prefix in SYNTHETIC_PREFIXES):
        return "runtime_generated"

    # Scheduled executions have their own non-Telegram session sources and are
    # excluded above. Do not classify conversational Telegram text by phrases it
    # happens to quote; those phrases are legitimate subjects of discussion.
    return None


def _normalize_quote(text: str) -> str:
    # NFC first: NFC vs NFD spellings of the same visible text (e.g. é as one
    # codepoint vs base + combining accent) must compare equal, both for quote
    # containment and duplicate thread labels. Whitespace is collapsed after.
    normalized = unicodedata.normalize("NFC", text)
    # Only true Unicode whitespace collapses. Python's \s also matches file/record
    # separators (U+001C–U+001F), which let a control character "match" a space in
    # user_text — a quote that is not literally present.
    return _WS_RE.sub(" ", normalized).strip()


def _transport_text(text: str) -> str:
    """Remove Hermes' multimodal JSON envelope without decoding media payloads.

    Historical message rows can contain ``NUL + json:`` followed by content
    parts. Image data URLs make those rows hundreds of kilobytes even when
    the owner authored one sentence. Only text parts belong in the semantic packet;
    binary/data-URL parts remain represented by their text attachment markers.
    """
    raw = text.strip()
    if not raw.startswith("\x00json:"):
        return raw
    try:
        parts = json.loads(raw[len("\x00json:") :])
    except json.JSONDecodeError as exc:
        raise ValueError("transport_envelope_invalid") from exc
    if not isinstance(parts, list):
        raise ValueError("transport_envelope_invalid")
    text_parts: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            text_parts.append(part["text"])
    return "\n".join(text_parts).strip()


def _authored_payload(text: str) -> tuple[str, str | None]:
    """Separate transport/context wrappers from owner-authored text.

    Known wrappers are parsed narrowly. Unknown or structurally ambiguous
    attachment wrappers fail closed so machine captions and document prose can
    never become quote-level evidence.
    """
    raw = _transport_text(text)
    if not raw:
        return "[non-text attachment]", None
    raw = _strip_leading_runtime_notes(raw)
    if not raw:
        return "", None
    bare = _LEADING_TAG_RE.sub("", raw, count=1)
    if any(raw.startswith(prefix) or bare.startswith(prefix) for prefix in SYNTHETIC_PREFIXES):
        return "", None

    reply_context: str | None = None
    had_reply_wrapper = raw.startswith("[Replying to:")
    if had_reply_wrapper:
        marker = '"]\n\n'
        # The first unescaped terminator closes the assistant quotation. A later
        # identical sequence may belong to the owner's own payload.
        pos = -1
        escaped = False
        for index in range(len("[Replying to:"), len(raw) - len(marker) + 1):
            char = raw[index]
            if char == "\\" and not escaped:
                escaped = True
                continue
            if raw.startswith(marker, index) and not escaped:
                pos = index
                break
            escaped = False
        if pos < 0:
            return "", None
        reply_context = raw[len("[Replying to:") : pos + 2].strip()
        raw = raw[pos + len(marker) :].strip()

    voice_match = re.match(
        r'^\[The user sent (?:a voice|an audio) message~ Here\'s what they said: "',
        raw,
    )
    if voice_match:
        marker = '"]\n\n'
        boundary = raw.find(marker, voice_match.end())
        if boundary < 0:
            if not raw.endswith('"]'):
                return "", reply_context
            transcript = raw[voice_match.end() : -2]
            suffix = ""
        else:
            transcript = raw[voice_match.end() : boundary]
            suffix = raw[boundary + len(marker) :].strip()
            suffix = _LEADING_TAG_RE.sub("", suffix, count=1).strip()
            suffix = _TRAILING_TAG_RE.sub("", suffix).strip()
        transcript = transcript.replace(r'\"', '"').strip()
        if re.fullmatch(SENDER_TAG, suffix):
            suffix = ""
        authored_parts = [part for part in (transcript, suffix) if part]
        if not authored_parts:
            return "[non-text attachment]", reply_context
        return "\n\n".join(authored_parts), reply_context

    # Machine-generated image descriptions are context, never owner-authored
    # evidence. There is no durable historical boundary after these captions.
    if re.search(r"\[The user sent (?:an image|images|multiple images)", raw):
        return "", reply_context

    if raw.startswith("[The user sent"):
        if "[Content of " in raw:
            return "", reply_context
        # Non-inlined attachment wrappers are one generated header line followed
        # by an explicit sender-tagged instruction. Anything else is ambiguous.
        boundary = raw.find("]\n\n")
        if boundary < 0:
            return "", reply_context
        suffix = raw[boundary + 3 :].strip()
        if not _LEADING_TAG_RE.match(suffix):
            return "", reply_context
        raw = _LEADING_TAG_RE.sub("", suffix, count=1).strip()
        if not raw:
            return "[non-text attachment]", reply_context

    if had_reply_wrapper:
        raw = _TRAILING_TAG_RE.sub("", raw).strip()
        if raw.startswith('"') and raw.endswith('"]'):
            raw = raw[1:-2].strip()
        elif len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
            raw = raw[1:-1].strip()

    # A leading sender tag belongs to Hermes transport. Later bracketed lines are user
    # content and must not truncate the message.
    raw = _LEADING_TAG_RE.sub("", raw, count=1).strip()
    raw = re.sub(
        r"(?m)^\[(?:Image attached at|Replied-to image)[^\]]*\]\s*$",
        "[image attachment]",
        raw,
    ).strip()
    # A trailing bare sender tag is transport residue after an appended runtime
    # note; remove it so the note itself can be recognized and stripped.
    # Repeat: transport can append multiple note+tag blocks after the payload.
    while True:
        stripped = _TRAILING_TAG_RE.sub("", raw).strip()
        stripped = _strip_trailing_runtime_notes(stripped).strip()
        if stripped == raw:
            break
        raw = stripped
    return (raw or "[non-text attachment]"), reply_context


def not_injected_sql(db: sqlite3.Connection, alias: str = "m") -> str:
    """SQL clause excluding injected display kinds, when this state.db records them."""
    columns = {r[1] for r in db.execute("PRAGMA table_info(messages)")}
    if "display_kind" not in columns:
        return ""
    kinds = ", ".join(f"'{k}'" for k in INJECTED_DISPLAY_KINDS)
    return f" AND COALESCE({alias}.display_kind, '') NOT IN ({kinds})"


def injected_sql(db: sqlite3.Connection, alias: str = "m") -> str | None:
    """SQL clause selecting only injected display kinds; None when state.db does not record them."""
    columns = {r[1] for r in db.execute("PRAGMA table_info(messages)")}
    if "display_kind" not in columns:
        return None
    kinds = ", ".join(f"'{k}'" for k in INJECTED_DISPLAY_KINDS)
    return f" AND COALESCE({alias}.display_kind, '') IN ({kinds})"


def authored_text(state_db: str | Path, session_id: str, message_id: int) -> str | None:
    """owner-authored payload of one primary user message, or None if not eligible."""
    db = sqlite3.connect(f"file:{Path(state_db).resolve()}?mode=ro", uri=True)
    try:
        row = db.execute(
            "SELECT m.content, s.source FROM messages m JOIN sessions s ON s.id = m.session_id"
            " WHERE m.id = ? AND m.session_id = ? AND m.role = 'user'"
            " AND (m.active = 1 OR m.compacted = 1)" + not_injected_sql(db),
            (message_id, session_id)).fetchone()
    finally:
        db.close()
    if row is None:
        return None
    text = _clean_text(row[0])
    if _synthetic_reason(text, str(row[1])) is not None:
        return None
    try:
        authored, _ = _authored_payload(text)
    except ValueError:
        return None
    if not authored or authored in ("[non-text attachment]",):
        return None
    return authored


class StateDbVerifier:
    """Verify `hermes:<session_id>:<message_id>` + quote against owner-authored text."""

    def __init__(self, state_db: str | Path):
        self.state_db = Path(state_db)

    def __call__(self, locator: str, quote: str) -> bool:
        match = LOCATOR_RE.fullmatch(locator or "")
        normalized = _normalize_quote(quote or "")
        if match is None or not MIN_QUOTE_CHARS <= len(normalized) <= MAX_QUOTE_CHARS:
            return False
        try:
            authored = authored_text(self.state_db, match["session"], int(match["message"]))
        except sqlite3.Error:
            return False
        return authored is not None and normalized in _normalize_quote(authored)


def find_authored(state_db: str | Path, session_id: str, quote: str, scan: int = 200) -> str | None:
    """Locator of the newest owner-authored message in `session_id` that contains `quote`.

    Lets a caller that knows only its session (a Hermes tool call) cite the owner's words;
    the returned locator passes `StateDbVerifier` by construction. Scans the newest
    `scan` user messages of that session only.
    """
    normalized = _normalize_quote(quote or "")
    if not session_id or not MIN_QUOTE_CHARS <= len(normalized) <= MAX_QUOTE_CHARS:
        return None
    db = sqlite3.connect(f"file:{Path(state_db).resolve()}?mode=ro", uri=True)
    try:
        ids = [r[0] for r in db.execute(
            "SELECT id FROM messages WHERE session_id = ? AND role = 'user'"
            " AND (active = 1 OR compacted = 1) ORDER BY id DESC LIMIT ?", (session_id, scan))]
    except sqlite3.Error:
        return None
    finally:
        db.close()
    for message_id in ids:
        locator = f"hermes:{session_id}:{message_id}"
        if LOCATOR_RE.fullmatch(locator) and StateDbVerifier(state_db)(locator, quote):
            return locator
    return None


# the owner's review replies (the memory-review skill's grammar). Compared case-folded, without
# surrounding punctuation.
# An assent is one or more of these words, in any order, with optional fillers ("yes please",
# "да, давай", "ok all", "ок 👍"); 👍 ✅ 👌 count as "ok". At least one assent word is needed.
ASSENT_WORDS = frozenset((
    "ok", "okay", "k", "yes", "yep", "yeah", "sure", "fine", "go", "doit", "apply",
    "ок", "окей", "да", "ага", "угу", "го", "давай", "хорошо", "ладно", "конечно", "применяй"))
ASSENT_FILLERS = frozenset(("please", "all", "все", "всё", "пожалуйста"))
_ASSENT_EMOJI = ("👍", "✅", "👌")
_ASSENT_PHRASES = (("go ahead", "go"), ("do it", "doit"))
UNDO_REPLIES = frozenset(("undo", "undo it", "undo that", "undo please", "revert",
                          "отмени", "отмени это", "верни", "откати"))
DROP_WORDS = ("drop", "reject", "удали", "убери", "выкинь")
# Anywhere outside an edit's text, any of these refuses the whole reply.
NEGATION_WORDS = frozenset((
    "no", "not", "nope", "nah", "never", "don't", "dont", "stop", "skip", "wait", "cancel", "later",
    "нет", "не", "ни", "никогда", "отмена", "стоп", "хватит", "подожди", "погоди", "потом", "пропусти",
    "пропустить"))
_EDGE_RE = re.compile(r"^[\W_]+|[\W_]+$")
_EDIT_START_RE = re.compile(r"(?:^|[\s,;])(\d{1,3})\s*:")
_EDIT_SPLIT_RE = re.compile(r"[;\n]+\s*(?=\d{1,3}\s*:)")
_EDIT_RE = re.compile(r"^(\d{1,3})\s*:\s*(\S.*)$", re.S)
_NUMS = r"\d{1,3}(?:\s*(?:,|and|и)?\s*\d{1,3})*"
_DROP_RE = re.compile(r"(?:^|[\s,;:.!])(?:" + "|".join(DROP_WORDS) + r")\s+(" + _NUMS + r")[\s.!,;]*$")


def reply_key(text: str) -> str:
    return " ".join(_EDGE_RE.sub("", unicodedata.normalize("NFC", text).casefold()).split())


def _assent_head(head: str) -> tuple[bool, list[int]] | None:
    """(assented, numbers to drop) of a reply's part before any edits, or None if not the grammar."""
    text = unicodedata.normalize("NFC", head).casefold()
    for emoji in _ASSENT_EMOJI:
        text = text.replace(emoji, " ok ")
    text = re.sub(r"[\ufe0f\U0001f3fb-\U0001f3ff]", "", text)  # emoji variation and skin tone
    for phrase, word in _ASSENT_PHRASES:
        text = re.sub(rf"\b{phrase}\b", word, text)
    drop = _DROP_RE.search(text)
    words_part = text[:drop.start()] if drop else text
    if re.sub(r"[\w'\s,;:.!]", "", words_part):
        return None  # anything else (another emoji, a symbol) is not the grammar
    words = re.findall(r"[\w']+", words_part)
    if any(w not in ASSENT_WORDS | ASSENT_FILLERS for w in words):
        return None
    assent = any(w in ASSENT_WORDS for w in words)
    if words and not assent:
        return None  # fillers alone ("please", "all") assent to nothing
    return assent, [int(n) for n in re.findall(r"\d+", drop[1])] if drop else []


def is_assent(message: str) -> bool:
    """The whole message is a plain assent ("ok", "yes please", "👍")."""
    head = _assent_head(message)
    return head is not None and head[0] and not head[1] and not _negated(message)


def is_undo(message: str) -> bool:
    return reply_key(message) in UNDO_REPLIES and not _negated(message)


def _negated(text: str) -> bool:
    words = re.findall(r"[\w']+", unicodedata.normalize("NFC", text).casefold().replace("’", "'"))
    return any(w in NEGATION_WORDS or w.endswith("n't") for w in words)


def parse_review_reply(message: str) -> dict | None:
    """the owner's reply to a review round, or None when it is not the grammar.

    `ok` / `ok, drop 3 5` / `3: new text` (edits last, one per line or after `;`), or a
    combination. A negation or refusal word anywhere outside an edit's text refuses it.
    """
    text = unicodedata.normalize("NFC", message or "").strip()
    start = _EDIT_START_RE.search(text)
    head, tail = (text[:start.start()], text[start.start():]) if start else (text, "")
    edits = {}
    for part in _EDIT_SPLIT_RE.split(re.sub(r"^[\s,;]+", "", tail)) if tail else []:
        match = _EDIT_RE.match(part.strip())
        if match is None or _CLOCK_RE.match(part.strip()) or _not_an_edit(match[2]):
            return None
        edits[int(match[1])] = match[2].strip()
    parsed = None if _negated(head) else _assent_head(head)
    if parsed is None or not (parsed[0] or parsed[1] or edits):
        return None
    return {"assent": parsed[0], "drop": parsed[1], "edits": edits}


_CLOCK_RE = re.compile(r"^\d{1,2}:\d{2}")  # "10:30 ..." is a time, not an edit of item 10
_DROP_CLAUSE_RE = re.compile(r"(?:^|[,;\n])\s*(?:" + "|".join(DROP_WORDS) + r")\s+\d")


def _not_an_edit(text: str) -> bool:
    """Edit text that is really a reply ("3: no", "1: wait, don't…", "3: ok", "x; drop 4")."""
    words = re.findall(r"[\w']+", unicodedata.normalize("NFC", text).casefold())
    return (not words or words[0] in NEGATION_WORDS or is_assent(text) or reply_key(text) in DROP_WORDS
            or _DROP_CLAUSE_RE.search(text.casefold()) is not None)


def _refusal(message: str) -> bool:
    # A message made only of refusal words ("no, stop") approves nothing, even as a free quote.
    words = re.findall(r"\w+", reply_key(message))
    return bool(words) and all(w in NEGATION_WORDS | {"this", "that", "it", "это"} for w in words)


def _completed_reply_sql(db: sqlite3.Connection) -> str:
    # A row with tool calls is the current turn at work; a reply with text and no tool calls ends it.
    columns = {r[1] for r in db.execute("PRAGMA table_info(messages)")}
    tools = " AND (tool_calls IS NULL OR trim(tool_calls) IN ('', '[]', 'null'))" if "tool_calls" in columns else ""
    return ("SELECT 1 FROM messages WHERE session_id = ? AND role = 'assistant' AND id > ?"
            " AND (active = 1 OR compacted = 1) AND trim(coalesce(content, '')) != ''" + tools + " LIMIT 1")


def _turn_message(state_db: str | Path, session_id: str) -> tuple[str, str] | None:
    """(locator, owner-authored text) of the message that opened the session's current turn.

    The newest user row (injected rows count) must be owner-authored, and no completed
    assistant reply may follow it (the turn's own tool-call rows may).
    """
    try:
        db = sqlite3.connect(f"file:{Path(state_db).resolve()}?mode=ro", uri=True)
        try:
            newest = db.execute("SELECT id FROM messages WHERE session_id = ? AND role = 'user'"
                                " AND (active = 1 OR compacted = 1) ORDER BY id DESC LIMIT 1",
                                (session_id,)).fetchone()
            if newest is None or db.execute(_completed_reply_sql(db), (session_id, newest[0])).fetchone():
                return None
        finally:
            db.close()
        authored = authored_text(state_db, session_id, newest[0])
    except sqlite3.Error:
        return None
    locator = f"hermes:{session_id}:{newest[0]}"
    if authored is None or LOCATOR_RE.fullmatch(locator) is None:
        return None  # an injected turn (kanban wake, cron delivery) is not the owner's answer
    return locator, authored


def turn_approval(state_db: str | Path, session_id: str, quote: str, mode: str = "free",
                  short_reply=None) -> str | None:
    """Locator of the approving message (`approval` without the parsed reply)."""
    got = approval(state_db, session_id, quote, mode, short_reply)
    return got[0] if got else None


def approval(state_db: str | Path, session_id: str, quote: str, mode: str = "free",
             short_reply=None) -> tuple[str, dict | None] | None:
    """(locator, parsed review reply or None) of the owner's approving message in this turn, or None.

    The caller binds the parsed reply to the action (assent for apply, drops for reject).

    The message must open the session's current turn (`_turn_message`), so an approval never
    outlives the turn it was given in. By `mode`:
    - `review` (apply, accept, reject): `quote` is the whole message and it parses as the
      review grammar (`parse_review_reply`);
    - `undo`: the whole message is an undo word ("undo", "отмени");
    - `free` (edit, forget, undo_auto): `quote` is in the message (MIN_QUOTE_CHARS..
      MAX_QUOTE_CHARS), or, when shorter, the whole message and accepted by `short_reply`
      (`is_assent`, `is_undo`; None: no short reply); a message made only of refusals
      ("no, stop") approves nothing.
    The claim-quote rules (`StateDbVerifier`, `find_authored`) are separate and unchanged.
    """
    normalized = _normalize_quote(quote or "")
    if not session_id or not normalized or len(normalized) > MAX_QUOTE_CHARS:
        return None
    turn = _turn_message(state_db, session_id)
    if turn is None:
        return None
    locator, authored = turn
    message = _normalize_quote(authored)
    reply = parse_review_reply(message)
    if mode == "review":
        ok = normalized == message and reply is not None
    elif mode == "undo":
        ok = normalized == message and is_undo(message)
    elif _refusal(message):
        ok = False
    elif len(normalized) >= MIN_QUOTE_CHARS:
        ok = normalized in message
    else:
        ok = normalized == message and short_reply is not None and short_reply(message)
    return (locator, reply) if ok else None


def previous_authored(state_db: str | Path, locator: str, scan: int = 20) -> str | None:
    """Locator of the owner's authored message right before `locator` in the same session."""
    match = LOCATOR_RE.fullmatch(locator or "")
    if match is None:
        return None
    try:
        db = sqlite3.connect(f"file:{Path(state_db).resolve()}?mode=ro", uri=True)
        try:
            ids = [r[0] for r in db.execute(
                "SELECT id FROM messages WHERE session_id = ? AND role = 'user' AND id < ?"
                " AND (active = 1 OR compacted = 1) ORDER BY id DESC LIMIT ?",
                (match["session"], int(match["message"]), scan))]
        finally:
            db.close()
        for message_id in ids:
            if authored_text(state_db, match["session"], message_id) is not None:
                return f"hermes:{match['session']}:{message_id}"
    except sqlite3.Error:
        return None
    return None
