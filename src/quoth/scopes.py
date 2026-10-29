"""Scope labels, recall tiers and built-in exclusions, from the package's ``scopes.toml``.

Scope labels are metadata and never filter the index. Each declared scope has a recall
tier: ``auto`` (may appear in automatic recall) or ``explicit`` (served only to a caller
that asks for it). Folders that must not become evidence, such as ``ASSISTANT/`` (notes
the assistant wrote), are built-in exclusions through ``exclude_dirs``; hidden folders and
notes (a leading dot), which Obsidian does not show either, are always excluded. A vault's
``.quothignore`` adds to these (``vault.py``).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files

RECALL_TIERS = ("auto", "explicit")


@dataclass(frozen=True)
class ScopeConfig:
    default: str
    default_recall: tuple[str, ...]
    rules: tuple[tuple[str, tuple[str, ...]], ...]
    exclude_dirs: frozenset[str]
    recall: tuple[tuple[str, str], ...] = ()  # (scope, tier) per declared scope

    @property
    def scopes(self) -> tuple[str, ...]:
        return tuple(s for s, _ in self.recall)

    def skip_dir(self, name: str) -> bool:
        """True for a directory that ingest never enters: hidden or in `exclude_dirs`."""
        return name.startswith(".") or name in self.exclude_dirs

    @staticmethod
    def is_note(name: str) -> bool:
        """A file ingest reads: a Markdown note whose name is not hidden."""
        return name.endswith(".md") and not name.startswith(".")

    def scope_for(self, rel_path: str) -> str:
        for scope, patterns in self.rules:
            for pat in patterns:
                if pat.endswith("/"):
                    if rel_path.startswith(pat):
                        return scope
                elif rel_path == pat:
                    return scope
        return self.default


@lru_cache(maxsize=1)
def load_config() -> ScopeConfig:
    data = tomllib.loads(files(__package__).joinpath("scopes.toml").read_text("utf-8"))
    rules = tuple((r["scope"], tuple(r["patterns"])) for r in data.get("rule", []))
    declared = data.get("scope", {})
    recall = []
    for name in sorted(declared):
        tier = declared[name].get("recall", "auto")
        if tier not in RECALL_TIERS:
            raise ValueError(f"unknown recall tier in scopes.toml: {name}: {tier!r}")
        recall.append((name, tier))
    cfg = ScopeConfig(
        default=data["default"],
        default_recall=tuple(data["default_recall"]),
        rules=rules,
        exclude_dirs=frozenset(data.get("ingest", {}).get("exclude_dirs", [])),
        recall=tuple(recall),
    )
    for s in (cfg.default, *cfg.default_recall, *(r[0] for r in rules)):
        if s not in cfg.scopes:
            raise ValueError(f"unknown scope in scopes.toml: {s!r}")
    return cfg


def scope_for(rel_path: str) -> str:
    return load_config().scope_for(rel_path)


def recall_tier(scope: str) -> str:
    """``"auto"`` or ``"explicit"`` for a declared scope; ``"auto"`` for an unknown label."""
    return dict(load_config().recall).get(scope, "auto")


def explicit_scopes() -> frozenset[str]:
    """The scopes kept out of automatic recall (served only on an explicit request)."""
    return frozenset(s for s, tier in load_config().recall if tier == "explicit")


SCOPES = load_config().scopes
