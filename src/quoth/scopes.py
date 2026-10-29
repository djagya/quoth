"""Scope labels and ingestion exclusions, loaded from the package's ``scopes.toml``.

Scope labels are metadata only and never filter retrieval. Folders that must not
become evidence, such as ``ASSISTANT/`` (notes the assistant wrote), are skipped at
ingestion through ``exclude_dirs``; hidden folders and notes (a leading dot), which
Obsidian does not show either, are always skipped.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files

SCOPES = ("general", "work")


@dataclass(frozen=True)
class ScopeConfig:
    default: str
    default_recall: tuple[str, ...]
    rules: tuple[tuple[str, tuple[str, ...]], ...]
    exclude_dirs: frozenset[str]

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
    cfg = ScopeConfig(
        default=data["default"],
        default_recall=tuple(data["default_recall"]),
        rules=rules,
        exclude_dirs=frozenset(data.get("ingest", {}).get("exclude_dirs", [])),
    )
    for s in (cfg.default, *cfg.default_recall, *(r[0] for r in rules)):
        if s not in SCOPES:
            raise ValueError(f"unknown scope in scopes.toml: {s!r}")
    return cfg


def scope_for(rel_path: str) -> str:
    return load_config().scope_for(rel_path)
