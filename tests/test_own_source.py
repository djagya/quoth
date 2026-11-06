"""The tests import this checkout's `quoth`, never another checkout's editable install.

An editable install points the interpreter at one checkout; tests run from another (a git
worktree) would test that one's code. `pythonpath` in pyproject.toml puts this checkout's
`src` and root first on sys.path, and tests/conftest.py passes the same to subprocesses."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import quoth
from quoth import assets

ROOT = Path(__file__).resolve().parents[1]


def _first_quoth(paths) -> str:
    return next(p for p in paths if (Path(p or ".") / "quoth" / "__init__.py").is_file())


def test_quoth_is_imported_from_this_checkout():
    assert Path(quoth.__file__).resolve().is_relative_to(ROOT / "src"), quoth.__file__
    assert Path(_first_quoth(sys.path)).resolve() == ROOT / "src"
    assert str(ROOT) in sys.path  # hermes_plugin/ and eval/ resolve from the root too
    assert assets.hermes_plugin_dir().is_relative_to(ROOT / "hermes_plugin")


def test_subprocesses_import_this_checkout_too():
    assert os.environ["PYTHONPATH"].split(os.pathsep)[:2] == [str(ROOT / "src"), str(ROOT)]
    out = subprocess.run([sys.executable, "-c", "import quoth; print(quoth.__file__)"],
                         capture_output=True, text=True, check=True).stdout.strip()
    assert Path(out).resolve().is_relative_to(ROOT / "src"), out
    script = Path(sys.executable).parent / "quoth"
    if script.exists():  # the console script found on PATH is this interpreter's
        assert Path(shutil.which("quoth")) == script
