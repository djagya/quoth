"""Default locations, resolved from the environment at call time.

quoth keeps all of its state under one home directory:

- ``QUOTH_HOME``: quoth's state (snapshot, ledger, episodes, socket, receipts).
  Default: ``$XDG_DATA_HOME/quoth``, else ``~/.local/share/quoth``.
- ``QUOTH_MODEL_CACHE``: local ONNX model files. Default: ``$QUOTH_HOME/models``.
- ``HERMES_HOME``: the Hermes agent's home, whose ``state.db`` holds chat history.
  Default: ``~/.hermes``.

Every function reads the environment when called, so tests and operators can point
quoth elsewhere without reloading modules. Nothing here creates directories.
"""
from __future__ import annotations

import os
from pathlib import Path

__all__ = ["home", "model_cache", "hermes_home", "state_file", "hermes_state_db"]


def home() -> Path:
    if env := os.environ.get("QUOTH_HOME"):
        return Path(env).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base).expanduser() / "quoth"


def model_cache() -> Path:
    if env := os.environ.get("QUOTH_MODEL_CACHE"):
        return Path(env).expanduser()
    return home() / "models"


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()


def state_file(name: str) -> Path:
    """A file directly under ``QUOTH_HOME``, e.g. ``state_file("ledger.db")``."""
    return home() / name


def hermes_state_db() -> Path:
    return hermes_home() / "state.db"
