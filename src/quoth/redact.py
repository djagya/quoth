"""Credential redaction for chat text: tokens, keys, passwords, URI credentials, IBAN and
card numbers. Hygiene, not privacy: the episode lanes store what remains verbatim."""

from __future__ import annotations

import re

# NOTE: separators are a single literal char (: = - or whitespace run); a
# character class like [:=] also swallows the value's first char.
_SEP = r"([ \t]*[=:][ \t]*|[ \t]+)"
_REDACTIONS: list[tuple[str, re.Pattern[str]]] = [
    # Bearer + optional whitespace + token; with whitespace an 8+ char token
    # suffices, concatenated (no whitespace) requires 20+ so prose like
    # "the bearer of" is untouched.
    ("bearer", re.compile(
        r"(?i)\b(bearer)(\s+[A-Za-z0-9\-._~+/]{8,}={0,2}|[A-Za-z0-9\-._~+/]{20,}={0,2})")),
    ("api_key_label", re.compile(
        r"(?i)((?:api|secret|access|auth|private)[-_ ]?(?:key|token|password|pwd|secret))"
        + _SEP + r"([A-Za-z0-9\-._~+/]{8,}={0,2})")),
    ("sk_token", re.compile(r"\b(sk-[A-Za-z0-9\-_]{10,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9\-]{10,})")),
    ("password_pair", re.compile(
        r"(?i)\b(password|passwd|pwd|пароль)\b" + _SEP + r"(\S{4,})")),
    # Password class includes '@' and is greedy: the match backtracks to the
    # LAST '@' in the run, so `user:p@ss@host` redacts the full userinfo even
    # when the password contains '@'. Worst case it swallows the host too —
    # over-redaction, never a leak. '/' is excluded so the match cannot run
    # past the path into a later '@'.
    ("uri_credentials", re.compile(
        r"(?i)\b([a-z][a-z0-9+.\-]{1,15}://)([A-Za-z0-9._%\-]{1,}):([A-Za-z0-9._%\-@]{1,})@")),
    ("iban", re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,3})?\b")),
    ("card", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
]
_PLACEHOLDER = "[REDACTED:%s]"


def redact(text: str) -> tuple[str, int]:
    """Replace credential-looking fragments. Returns (text, hit_count)."""
    hits = 0
    for label, pat in _REDACTIONS:
        def _sub(m: re.Match, label: str = label) -> str:
            g = m.groups()
            if g and g[0] and g[0].lower() in (
                "api", "secret", "access", "auth", "private", "password", "passwd", "pwd",
                "пароль", "bearer",
            ):
                # label word + separator kept, value replaced
                return f"{g[0]}{g[1] if len(g) > 2 else ''}{_PLACEHOLDER % label}"
            if label == "uri_credentials":
                return f"{g[0]}{g[1]}{_PLACEHOLDER % label}"
            return _PLACEHOLDER % label
        text, n = pat.subn(_sub, text)
        hits += n
    return text, hits
