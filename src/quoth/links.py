"""Links between notes: extraction from a note's body and Obsidian-style resolution.

Recognised forms: wikilinks ``[[Target]]``, ``[[Target|alias]]``, ``[[Target#Heading]]``,
embeds ``![[Target]]``, and relative Markdown links ``[text](path.md)`` (``#heading``
allowed, ``%20`` and ``<...>`` forms decoded). Links in frontmatter, fenced code and
inline code are ignored; URLs and links to non-``.md`` files in Markdown syntax are not
links between notes.

Resolution (``Resolver.resolve``) follows Obsidian: an exact vault path first (with or
without ``.md``, a relative ``./``/``../`` path from the linking note, case-insensitive
when unique), else a unique match on the path's tail (``[[Note]]`` or ``[[Sub/Note]]``
against ``.../Sub/Note.md``). The status says how it went: ``resolved`` (the path is
returned), ``missing``, ``ambiguous``, or ``excluded`` (the target is a note excluded from
the index; its path is never stored).
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from urllib.parse import unquote

from .chunker import _FENCE, parse_frontmatter

_WIKI = re.compile(r"(!?)\[\[([^\[\]\n]+?)\]\]")
_MD = re.compile(r"(?<![!\\])\[(?:[^\[\]\n]|\[[^\[\]\n]*\])*\]\(\s*(<[^<>\n]+>|[^\s()<>]+)"
                 r"(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'))?\s*\)")
_INLINE_CODE = re.compile(r"(`+)(?:(?!\1).)+?\1")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")

# links.status: the link points at exactly one indexed note (`target_path` set), at no
# note, at several notes, or at a note excluded from the index (its path is not stored).
RESOLVED, MISSING, AMBIGUOUS, EXCLUDED = "resolved", "missing", "ambiguous", "excluded"
STATUSES = (RESOLVED, MISSING, AMBIGUOUS, EXCLUDED)


@dataclass(frozen=True)
class Link:
    kind: str          # "wikilink", "embed" or "markdown"
    target: str        # the link target as written, without heading and alias
    heading: str | None
    line: int          # 1-based line in the note
    raw: str           # the matched text, to place the link in a chunk


def _blank(m: re.Match) -> str:
    return " " * (m.end() - m.start())


def extract_links(content: str) -> list[Link]:
    """Every link in the note's body, in order of appearance."""
    lines = content.split("\n")
    _, body_start = parse_frontmatter(lines)
    out: list[Link] = []
    in_fence, fence_tok = False, ""
    for idx in range(body_start, len(lines)):
        line = lines[idx].rstrip("\r")
        fm = _FENCE.match(line)
        if fm:
            if not in_fence:
                in_fence, fence_tok = True, fm.group(1)
            elif fm.group(1) == fence_tok:
                in_fence = False
            continue
        if in_fence or ("[" not in line):
            continue
        scan = _INLINE_CODE.sub(_blank, line)
        no = idx + 1
        found: list[tuple[int, Link]] = []
        for m in _WIKI.finditer(scan):
            inner = m.group(2)
            left = inner.split("|", 1)[0]
            target, _, heading = left.partition("#")
            target = target.rstrip("\\").strip()
            heading = heading.rstrip("\\").strip() or None
            if not target and not heading:
                continue
            found.append((m.start(), Link("embed" if m.group(1) else "wikilink", target,
                                          heading, no, line[m.start():m.end()])))
        for m in _MD.finditer(scan):
            dest = m.group(1)
            if dest.startswith("<"):
                dest = dest[1:-1].strip()
            if _SCHEME.match(dest) or dest.startswith("#"):
                continue
            path, _, heading = dest.partition("#")
            if not unquote(path).lower().endswith(".md"):
                continue
            found.append((m.start(), Link("markdown", path, unquote(heading).strip() or None,
                                          no, line[m.start():m.end()])))
        out.extend(link for _, link in sorted(found, key=lambda item: item[0]))
    return out


def _key(path: str) -> str:
    path = path.lower()
    return path[:-3] if path.endswith(".md") else path


class Resolver:
    """Resolve link targets against the vault's notes.

    ``indexed`` are the notes in the snapshot; ``excluded`` are notes that exist but are
    excluded: they take part in matching (so a link to one is never resolved to a
    different note of the same name), but resolve to None.
    """

    def __init__(self, indexed, excluded=()):
        self.indexed = frozenset(indexed)
        self.all = self.indexed | frozenset(excluded)
        self._by_key: dict[str, list[str]] = {}
        self._by_name: dict[str, list[str]] = {}
        for p in sorted(self.all):
            k = _key(p)
            self._by_key.setdefault(k, []).append(p)
            self._by_name.setdefault(k.rsplit("/", 1)[-1], []).append(p)

    def _status(self, path: str) -> tuple[str, str | None]:
        return (RESOLVED, path) if path in self.indexed else (EXCLUDED, None)

    def _exact(self, path: str) -> tuple[str, str | None] | None:
        """Status for an exact vault path, with or without ``.md``; None when unknown."""
        for cand in (path, path + ".md"):
            if cand in self.all:
                return self._status(cand)
        hits = self._by_key.get(_key(path), [])
        if len(hits) == 1:
            return self._status(hits[0])
        return (AMBIGUOUS, None) if hits else None

    def resolve(self, kind: str, target: str, source: str) -> tuple[str, str | None]:
        """``(status, target_path)``; ``target_path`` is set only when resolved."""
        if not target:  # [[#Heading]]: a link into the linking note itself
            return self._status(source)
        if kind == "markdown":
            target = unquote(target)
        target = target.strip()
        base = posixpath.dirname(source)
        relative = kind == "markdown" or target.startswith(("./", "../"))
        if target.startswith("/"):
            candidates = [target.lstrip("/")]
        elif relative:
            candidates = [posixpath.join(base, target), target]
        else:
            candidates = [target]
        for cand in candidates:
            norm = posixpath.normpath(cand)
            if norm.startswith("../") or norm == "..":
                continue
            hit = self._exact(norm)
            if hit is not None:
                return hit
        tail = _key(posixpath.normpath(target).lstrip("./"))
        if not tail or tail.startswith("../"):
            return MISSING, None
        name = tail.rsplit("/", 1)[-1]
        hits = [p for p in self._by_name.get(name, [])
                if _key(p) == tail or _key(p).endswith("/" + tail)]
        if not hits:
            return MISSING, None
        return self._status(hits[0]) if len(hits) == 1 else (AMBIGUOUS, None)
