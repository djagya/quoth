"""Heading-aware chunker with line locators; frontmatter -> metadata; the embedder input
of a chunk (`passage_text`)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

WINDOW = 1200
OVERLAP = 200

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t#]*$")
_FENCE = re.compile(r"^[ \t]{0,3}(```|~~~)")
# A run of 200+ base64, base64url or hex characters: an embedded image, drawing data or a
# key. No word is that long, and nobody asks about the bytes.
_BLOB = re.compile(r"[A-Za-z0-9+/=_-]{200,}")


def _mostly_blob(text: str) -> bool:
    return 2 * sum(m.end() - m.start() for m in _BLOB.finditer(text)) > len(text)


@dataclass(frozen=True)
class Chunk:
    heading_path: str
    line_start: int  # 1-based, inclusive, in the original file
    line_end: int
    text: str


def _scalar(v: str):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        return [_scalar(x) for x in inner.split(",") if x.strip()] if inner else []
    return v


def parse_frontmatter(lines: list[str]) -> tuple[dict, int]:
    """Minimal YAML-subset parser. Returns (metadata, body_start_index)."""
    if not lines or lines[0].strip() != "---":
        return {}, 0
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            end = i
            break
    if end is None:
        return {}, 0
    meta: dict = {}
    key = None
    for raw in lines[1:end]:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        item = re.match(r"^\s+-\s*(.*)$", raw) or re.match(r"^-\s+(.*)$", raw)
        if item and key is not None:
            if not isinstance(meta.get(key), list):
                meta[key] = []
            meta[key].append(_scalar(item.group(1)))
            continue
        # Any top-level key (Obsidian property names may hold spaces or any script).
        kv = re.match(r"^([^\s:#][^:]*?)\s*:\s*(.*)$", raw)
        if kv:
            key = _scalar(kv.group(1))
            val = kv.group(2)
            meta[key] = _scalar(val) if val.strip() else []
    return meta, end + 1


# A sentence ends at . ! ? or … (optionally followed by closing quotes or brackets) and
# whitespace, or at a full-width 。！？; used to place cuts inside one over-long line (a
# long paragraph).
_SENTENCE_END = re.compile(r"[.!?…][\"'»”)\]]*\s+|[。！？][」』”)]*")
_SPACE = re.compile(r"\s+")


def _cut(t: str, start: int) -> int:
    """End of the piece of `t` that starts at `start`: at most WINDOW characters, cut after
    the last sentence in its second half, else at the last space there, else hard."""
    limit = start + WINDOW
    if limit >= len(t):
        return len(t)
    lo = start + WINDOW // 2
    ends = [m.end() for m in _SENTENCE_END.finditer(t, lo, limit)]
    if ends:
        return ends[-1]
    spaces = [m.start() for m in _SPACE.finditer(t, lo, limit)]
    return spaces[-1] if spaces else limit


def _resume(t: str, end: int) -> int:
    """Start of the next piece: repeat at most OVERLAP characters before `end`, from a
    sentence start if one is there, else from a word start."""
    lo = end - OVERLAP
    for pattern in (_SENTENCE_END, _SPACE):
        m = pattern.search(t, lo, end)
        if m and m.end() < end:
            return m.end()
    return lo


def _split_line(t: str) -> list[str]:
    out, start = [], 0
    while True:
        end = _cut(t, start)
        out.append(t[start:end].rstrip())
        if end >= len(t):
            return out
        start = _resume(t, end)


def _windows(lines: list[tuple[int, str]]) -> list[tuple[int, int, str]]:
    """Split (lineno, text) lines into ~WINDOW-char windows with ~OVERLAP overlap,
    snapped to line boundaries; a single over-long line is split at sentence or word
    boundaries. Every window holds at least one line the previous one did not."""
    text = "\n".join(t for _, t in lines)
    if len(text) <= WINDOW:
        return [(lines[0][0], lines[-1][0], text)]
    out: list[tuple[int, int, str]] = []
    i = 0
    n = len(lines)
    while i < n:
        no, t = lines[i]
        if len(t) > WINDOW:
            out.extend((no, no, piece) for piece in _split_line(t))
            i += 1
            continue
        size = 0
        j = i
        while j < n and len(lines[j][1]) <= WINDOW and (j == i or size + 1 + len(lines[j][1]) <= WINDOW):
            size += len(lines[j][1]) + (1 if j > i else 0)
            j += 1
        out.append((lines[i][0], lines[j - 1][0], "\n".join(t for _, t in lines[i:j])))
        if j >= n:
            break
        # Next window repeats up to ~OVERLAP chars of this one, but only as many lines
        # as fit beside the next new line (none before an over-long line).
        nxt = len(lines[j][1])
        k, back = j, 0
        while (k - 1 > i and back + len(lines[k - 1][1]) + 1 <= OVERLAP
               and back + len(lines[k - 1][1]) + 1 + nxt <= WINDOW):
            k -= 1
            back += len(lines[k][1]) + 1
        i = max(k, i + 1)
    return out


def chunk_markdown(content: str) -> tuple[dict, list[Chunk]]:
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    meta, body_start = parse_frontmatter(lines)

    sections: list[tuple[str, list[tuple[int, str]]]] = []
    stack: list[tuple[int, str]] = []
    cur: list[tuple[int, str]] = []
    cur_path = ""
    in_fence = False
    fence_tok = ""
    for idx in range(body_start, len(lines)):
        line = lines[idx].rstrip("\r")
        no = idx + 1
        fm = _FENCE.match(line)
        if fm:
            if not in_fence:
                in_fence, fence_tok = True, fm.group(1)
            elif fm.group(1) == fence_tok:
                in_fence = False
        h = None if in_fence or fm else _HEADING.match(line)
        if h:
            sections.append((cur_path, cur))
            level = len(h.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, h.group(2).strip()))
            cur_path = " > ".join(t for _, t in stack)
            cur = [(no, line)]
        else:
            cur.append((no, line))
    sections.append((cur_path, cur))

    chunks: list[Chunk] = []
    for path, sec in sections:
        # Trim blank lines at both ends so locators point at real content.
        while sec and not sec[-1][1].strip():
            sec = sec[:-1]
        while sec and not sec[0][1].strip():
            sec = sec[1:]
        if not sec:
            continue
        for ls, le, text in _windows(sec):
            # A window of only the heading line (a heading-only section, or a heading
            # before an over-long line) holds nothing beyond the heading path; a window
            # that is mostly encoded data holds nothing to retrieve.
            if (text.strip() and not (path and ls == le and _HEADING.match(text))
                    and not _mostly_blob(text)):
                chunks.append(Chunk(path, ls, le, text))
    return meta, chunks


MAX_ALIASES = 8
MAX_ALIAS_CHARS = 80


def note_aliases(metadata: str | None) -> list[str]:
    """The note's other names: frontmatter `aliases` (or `alias`), a list or one string,
    as Obsidian reads it. At most MAX_ALIASES names of at most MAX_ALIAS_CHARS each."""
    try:
        meta = json.loads(metadata) if metadata else {}
    except ValueError:
        return []
    value = meta.get("aliases", meta.get("alias")) if isinstance(meta, dict) else None
    out: list[str] = []
    for item in value if isinstance(value, list) else [value]:
        name = " ".join(item.split())[:MAX_ALIAS_CHARS] if isinstance(item, str) else ""
        if name and name not in out:
            out.append(name)
    return out[:MAX_ALIASES]


def passage_text(path: str, heading_path: str, text: str, metadata: str | None = None) -> str:
    """The embedder input of a chunk: its text under the note's title (and aliases) and its
    heading path, which give the chunk its context; encoded data runs are left out. It
    shapes every stored vector, so it lives in this index-contract file, never in a
    rebase-eligible one."""
    title = PurePosixPath(path).stem
    text = _BLOB.sub("…", text)
    names = [a for a in note_aliases(metadata) if a != title]
    head = f"{title} ({', '.join(names)})" if names else title
    if heading_path and heading_path != title:
        head = f"{head} — {heading_path}"
    return f"{head}\n{text}"
