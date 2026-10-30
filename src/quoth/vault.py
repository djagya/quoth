"""Which vault notes become evidence: built-in exclusions, ``.quothignore`` and frontmatter.

There is one exclusion path. A note is indexed only when all of these hold:

- no built-in rule excludes it: a hidden name (a leading dot) on its path, or a folder
  named in ``exclude_dirs`` (``scopes.toml``, the built-in defaults);
- no rule in ``<vault>/.quothignore`` (gitignore syntax, added to the defaults) ignores
  it or a folder above it;
- its YAML frontmatter does not say ``quoth: exclude``.

An excluded note is never chunked, embedded or stored, and one that was indexed before is
purged from the next snapshot: its sources, versions, chunks, vectors, lexical and link
rows, current and historical. Every reader of the vault (ingest, the freshness inventory)
uses ``walk`` below, so they agree on what is in.

``.quothignore`` supports the gitignore subset: blank lines and ``#`` comments, ``!``
negation (the last matching rule wins), a trailing ``/`` for folders only, ``*``, ``?``,
``[...]``, ``**`` (``**/x``, ``x/**``, ``a/**/b``), and anchoring: a pattern holding a ``/``
anywhere but at its end matches from the vault root, one without matches at any depth.
As in git, a note inside an ignored folder cannot be re-included. Matching is
case-sensitive.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .chunker import parse_frontmatter
from .scopes import ScopeConfig, load_config

IGNORE_FILE = ".quothignore"


@dataclass(frozen=True)
class _Rule:
    regex: re.Pattern
    negate: bool
    dir_only: bool


def _translate(body: str) -> str:
    """Regex for one gitignore pattern body (no leading/trailing slash, no `!`)."""
    out, i, n = [], 0, len(body)
    while i < n:
        at_seg = i == 0 or body[i - 1] == "/"
        if at_seg and body.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif at_seg and body[i:] == "**":
            out.append(".*")
            i += 2
        elif body[i] == "*":
            out.append("[^/]*")
            i += 1
        elif body[i] == "?":
            out.append("[^/]")
            i += 1
        elif body[i] == "[":
            j = body.find("]", i + 2 if body[i + 1:i + 2] in ("!", "^") else i + 1)
            if j == -1:
                out.append(re.escape("["))
                i += 1
                continue
            inner = body[i + 1:j]
            if inner[:1] in ("!", "^"):
                inner = "^" + inner[1:]
            out.append("[" + inner.replace("\\", "\\\\") + "]")
            i = j + 1
        elif body[i] == "\\" and i + 1 < n:
            out.append(re.escape(body[i + 1]))
            i += 2
        else:
            out.append(re.escape(body[i]))
            i += 1
    return "".join(out)


def _parse_line(line: str) -> _Rule | None:
    line = line.rstrip("\n").rstrip("\r")
    # Trailing spaces are dropped unless escaped.
    stripped = line.rstrip(" ")
    if stripped.endswith("\\") and len(stripped) < len(line):
        stripped += " "
    line = stripped
    if not line or line.startswith("#"):
        return None
    negate = line.startswith("!")
    if negate:
        line = line[1:]
    elif line.startswith("\\!") or line.startswith("\\#"):
        line = line[1:]
    dir_only = line.endswith("/")
    body = line.rstrip("/")
    if not body:
        return None
    anchored = "/" in body
    body = body.lstrip("/")
    if not body:
        return None
    prefix = "^" if anchored else "^(?:.*/)?"
    return _Rule(re.compile(prefix + _translate(body) + "$"), negate, dir_only)


class IgnoreRules:
    """The vault's exclusion rules: the built-in ones, then ``<vault>/.quothignore``.

    Built-in rules (hidden names, ``exclude_dirs`` from ``scopes.toml``) always apply: a
    ``!`` rule in ``.quothignore`` re-includes only what ``.quothignore`` itself ignored.
    ``sha256`` is that of ``.quothignore`` (None when the vault has none).
    """

    def __init__(self, text: str = "", sha256: str | None = None,
                 cfg: ScopeConfig | None = None):
        cfg = cfg or load_config()
        self.sha256 = sha256
        self.builtin = tuple(_parse_line(p) for p in
                             (".*", *(f"{d}/" for d in sorted(cfg.exclude_dirs))))
        self.rules = tuple(r for r in map(_parse_line, text.splitlines()) if r is not None)

    @classmethod
    def load(cls, vault: Path) -> IgnoreRules:
        path = Path(vault) / IGNORE_FILE
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ValueError(f"{IGNORE_FILE} must be a regular file")
        if not path.exists():
            return cls()
        raw = path.read_bytes()
        return cls(raw.decode("utf-8-sig", errors="replace"), hashlib.sha256(raw).hexdigest())

    @staticmethod
    def _match(rules, rel: str, is_dir: bool) -> bool:
        ignored = False
        for rule in rules:
            if rule.dir_only and not is_dir:
                continue
            if rule.regex.match(rel):
                ignored = not rule.negate
        return ignored

    def _ignored(self, rules, rel: str) -> bool:
        parts = rel.split("/")
        for k in range(1, len(parts)):
            if self._match(rules, "/".join(parts[:k]), True):
                return True
        return self._match(rules, rel, False)

    def ignored(self, rel: str) -> bool:
        """True when the file ``rel`` (vault-relative POSIX) or a folder above it is
        excluded by a built-in rule or by ``.quothignore``."""
        return self._ignored(self.builtin, rel) or self._ignored(self.rules, rel)


def ignore_rules_sha256(vault: Path) -> str | None:
    """SHA-256 of ``<vault>/.quothignore`` (None when absent); recorded in every manifest."""
    return IgnoreRules.load(vault).sha256


def path_excluded(rel: str, rules: IgnoreRules) -> bool:
    """True unless ``rel`` is a Markdown note that no rule excludes by its path."""
    return not rel.endswith(".md") or rules.ignored(rel)


def frontmatter_excluded(raw: bytes) -> bool:
    """True when the note's frontmatter says ``quoth: exclude``."""
    if not raw.startswith((b"---", b"\xef\xbb\xbf---")):
        return False
    text = raw.decode("utf-8-sig", errors="replace").replace("\r\n", "\n")
    meta, _ = parse_frontmatter(text.split("\n"))
    value = meta.get("quoth")
    values = value if isinstance(value, list) else [value]
    return any(isinstance(v, str) and v.strip().lower() == "exclude" for v in values)


@dataclass(frozen=True)
class Entry:
    """One Markdown file found in the vault. ``excluded`` is by path only; the
    frontmatter check needs the file's bytes and is up to the reader."""
    rel: str
    path: Path
    excluded: bool


def walk(vault: Path, rules: IgnoreRules, *, strict: bool = False):
    """Yield an ``Entry`` per ``.md`` file outside hidden folders, sorted, without reading.

    Folders excluded by ``exclude_dirs`` or ``.quothignore`` are listed (their notes come
    out with ``excluded=True``, so a link to one is known to point at an excluded note) but
    nothing in them is read. Symlinked folders are never entered. With ``strict``, an
    unreadable folder or a non-regular note that would be indexed raises ``ValueError``
    instead of being skipped, so a partial scan is never taken for deleted notes.
    """
    vault = Path(vault)

    def onerror(error: OSError):
        if not strict:
            return
        try:
            rel = Path(error.filename).relative_to(vault).as_posix()
        except (TypeError, ValueError):
            raise error from None
        if rel == "." or not path_excluded(rel + "/x.md", rules):
            raise error

    for root, dirs, names in os.walk(vault, followlinks=False, onerror=onerror):
        dirs[:] = sorted(d for d in dirs
                         if not d.startswith(".") and not (Path(root) / d).is_symlink())
        for name in sorted(names):
            if not name.endswith(".md"):
                continue
            path = Path(root) / name
            rel = path.relative_to(vault).as_posix()
            excluded = path_excluded(rel, rules)
            if not excluded and (path.is_symlink() or not path.is_file()):
                if strict:
                    raise ValueError("non-regular vault note is not supported")
                continue
            yield Entry(rel, path, excluded)
