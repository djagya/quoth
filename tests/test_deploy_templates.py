"""Host templates in deploy/: fixed install path, parameterized paths, valid shell, socket-mode control."""
from __future__ import annotations

from pathlib import Path
import re
import subprocess

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
SCRIPTS = [DEPLOY / "s6" / "quoth-sidecar" / "run", DEPLOY / "s6" / "quoth-sidecar" / "finish",
           DEPLOY / "cron" / "quoth-review-nudge.sh", DEPLOY / "install-release.sh"]
VERSIONED = re.compile(r"quoth-\d+\.\d+\.\d+")


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_shell_templates_parse_and_are_executable(path):
    assert subprocess.run(["sh", "-n", str(path)], check=False).returncode == 0
    assert path.stat().st_mode & 0o111


@pytest.mark.parametrize("path", [SCRIPTS[0], SCRIPTS[2], DEPLOY / "compose" / "quoth-control.yaml"],
                         ids=lambda p: p.name)
def test_templates_use_the_fixed_path_not_a_version(path):
    text = path.read_text()
    assert "/quoth-current" in text
    assert not VERSIONED.search(text)


def test_sidecar_template_keeps_its_host_flags():
    run = (DEPLOY / "s6" / "quoth-sidecar" / "run").read_text()
    assert "HOST VARIABLES" in run
    for var in ("QUOTH_PREFIX=", "QUOTH_HOME=", "HERMES_HOME=", "QUOTH_MODEL_CACHE="):
        assert var in run, var
    for flag in ("--socket", "--ledger", "--state-db", "--db", "--core-dir", "--episodes",
                 "--owner-episodes", "--episodes-interval", "--prefetch-log", "--prefetch-log-days"):
        assert f"  {flag} " in run, flag
    # The record-only prefetch log is on in the production template, 30 days, under the state home.
    assert "PREFETCH_LOG=$QUOTH_HOME/prefetch-log" in run and "PREFETCH_LOG_DAYS=30" in run
    assert 'chmod 0700 "$PREFETCH_LOG"' in run
    assert (DEPLOY / "s6" / "quoth-sidecar" / "timeout-finish").read_text().strip() == "35000"


def test_control_compose_is_socket_mode_in_the_webui_namespace():
    text = (DEPLOY / "compose" / "quoth-control.yaml").read_text()
    assert "network_mode: service:hermes-webui" in text
    assert "read_only: true" in text and "${HERMES_IMAGE_VOLUME:?}:size=1m" in text
    assert '"-m", "quoth.control"' in text
    assert "--quoth-socket" in text and "--ledger" not in text.split("command:", 1)[1].split("volumes:")[0]
    assert "--snapshot" not in text  # memory views only
    assert "HERMES_WEBUI_EXTENSION_DIR: ${QUOTH_PREFIX:?}/quoth-current/" in text


def test_control_compose_mounts_the_install_at_its_own_path():
    """The venv's interpreter links are absolute: the install and the state are mounted at the
    same path inside the containers as where they were built, never at a fixed container path."""
    text = (DEPLOY / "compose" / "quoth-control.yaml").read_text()
    assert text.count("- ${QUOTH_PREFIX_SOURCE:-${QUOTH_PREFIX:?}}:${QUOTH_PREFIX:?}:ro") == 2  # webui + control
    assert "- ${QUOTH_HOME_SOURCE:-${QUOTH_HOME:?}}:${QUOTH_HOME:?}:ro" in text
    assert 'entrypoint: ["${QUOTH_PREFIX:?}/quoth-current/bin/python"' in text
    assert "QUOTH_HOME: ${QUOTH_HOME:?}" in text and "${QUOTH_HOME:?}/quoth.sock" in text
    assert "/quoth/venvs" not in text and "/quoth/state" not in text and "/quoth/extension" not in text


def test_nudge_wrapper_calls_the_package():
    text = (DEPLOY / "cron" / "quoth-review-nudge.sh").read_text()
    assert 'exec "$QUOTH_PREFIX/quoth-current/bin/quoth" ledger' in text and text.rstrip().endswith("nudge")


def test_mcp_snippet_has_no_read_only_flag():
    text = (DEPLOY / "hermes-mcp-snippet.yaml").read_text()
    assert "- mcp" in text and "--read-only" not in text and "- --db" in text


def test_templates_carry_no_host_specific_paths_or_names():
    for path in DEPLOY.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text()
        for private in ("/opt/", "SMEM_", "BOX VARIABLES", "--read-only", "--golden",
                        "--quality-golden-v3"):
            assert private not in text, (path.name, private)
