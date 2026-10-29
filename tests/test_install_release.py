"""deploy/install-release.sh --activate: the fixed quoth-current path, atomic repoint, rollback."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "install-release.sh"


def fake_version(prefix: Path, version: str) -> Path:
    venv = prefix / f"quoth-{version}"
    (venv / "bin").mkdir(parents=True)
    python = venv / "bin" / "python"
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    python.chmod(0o755)
    cli = venv / "bin" / "quoth"
    cli.write_text("#!/bin/sh\nexit 0\n")
    cli.chmod(0o755)
    return venv


def activate(prefix: Path, version: str) -> subprocess.CompletedProcess:
    return subprocess.run(["sh", str(SCRIPT), "--activate", version, "--prefix", str(prefix)],
                          capture_output=True, text=True, timeout=60, check=False)


def test_activate_and_roll_back(tmp_path):
    fake_version(tmp_path, "1.0.0")
    fake_version(tmp_path, "1.1.0")
    current = tmp_path / "quoth-current"
    assert activate(tmp_path, "1.0.0").returncode == 0
    assert os.readlink(current) == "quoth-1.0.0"  # relative: same inside and outside a container
    assert current.resolve() == (tmp_path / "quoth-1.0.0").resolve()
    out = activate(tmp_path, "1.1.0")
    assert out.returncode == 0 and "quoth-current -> quoth-1.1.0" in out.stdout
    assert os.readlink(current) == "quoth-1.1.0"
    assert activate(tmp_path, "1.0.0").returncode == 0  # rollback = repoint
    assert os.readlink(current) == "quoth-1.0.0"
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".quoth-current")]


@pytest.mark.parametrize("version", ["9.9.9", "../evil", ""])
def test_activate_refuses_unknown_versions_and_keeps_the_link(tmp_path, version):
    fake_version(tmp_path, "1.0.0")
    assert activate(tmp_path, "1.0.0").returncode == 0
    out = activate(tmp_path, version)
    assert out.returncode != 0
    assert os.readlink(tmp_path / "quoth-current") == "quoth-1.0.0"


def test_activate_refuses_a_version_whose_interpreter_is_gone(tmp_path):
    venv = fake_version(tmp_path, "1.0.0")
    (venv / "bin" / "python").write_text("#!/bin/sh\nexit 127\n")
    out = activate(tmp_path, "1.0.0")
    assert out.returncode != 0 and not (tmp_path / "quoth-current").is_symlink()


def test_activate_never_replaces_a_real_directory(tmp_path):
    fake_version(tmp_path, "1.0.0")
    (tmp_path / "quoth-current").mkdir()
    out = activate(tmp_path, "1.0.0")
    assert out.returncode != 0 and "not a symlink" in out.stderr


def test_activate_takes_no_assets(tmp_path):
    out = subprocess.run(["sh", str(SCRIPT), "--activate", "1.0.0", "--assets", str(tmp_path), "--sums", "x",
                          "--prefix", str(tmp_path)], capture_output=True, text=True, timeout=60, check=False)
    assert out.returncode == 2


def test_prefix_is_required(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "QUOTH_PREFIX"}
    out = subprocess.run(["sh", str(SCRIPT), "--activate", "1.0.0"], capture_output=True, text=True,
                         timeout=60, check=False, env=env)
    assert out.returncode == 2 and "--prefix" in out.stderr
