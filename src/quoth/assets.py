"""Synthetic eval assets: the checkout's eval/ directory, or the copy packaged in the wheel."""
from __future__ import annotations

from pathlib import Path

_PACKAGE = Path(__file__).resolve().parent


def eval_dir() -> Path:
    # Source checkout: <repo>/src/quoth -> <repo>/eval. Installed wheel: quoth/_eval.
    checkout = _PACKAGE.parents[1] / "eval"
    if (checkout / "models-manifest.json").is_file():
        return checkout
    return _PACKAGE / "_eval"


def eval_path(name: str) -> Path:
    return eval_dir() / name


def hermes_plugin_dir() -> Path:
    """The Hermes "Quoth" provider package: checkout hermes_plugin/ or the wheel copy."""
    checkout = _PACKAGE.parents[1] / "hermes_plugin" / "quoth"
    if (checkout / "plugin.yaml").is_file():
        return checkout
    return _PACKAGE / "_hermes_plugin" / "quoth"


def webui_extension_dir() -> Path:
    """The Hermes WebUI extension (extensions.json + quoth/): checkout web/control-surface/
    or the wheel copy. Mount it read-only as the WebUI's extension directory."""
    checkout = _PACKAGE.parents[1] / "web" / "control-surface"
    if (checkout / "extensions.json").is_file():
        return checkout
    return _PACKAGE / "_webui_extension"
