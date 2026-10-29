"""Static wiring assertions for the gated delivery workflow.

The executable guard logic lives in ``scripts/release_guard.py`` and is
behaviour-tested hermetically in ``tests/test_release_guard.py`` (no GitHub
calls, no publication). These tests pin the workflow *wiring* that the release process
requires to stay intact: least privilege, full-SHA action pins, lock,
explicit env transport, manual gate, exact-commit checkout, and that no
inline guard copy remains in the YAML.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
YAML = REPO / '.github' / 'workflows' / 'verify-release.yml'


def workflow_text():
    return YAML.read_text()


def deliver_block():
    text = workflow_text()
    assert '  deliver:' in text
    return text.split('  deliver:', 1)[1]


def test_every_action_pinned_to_full_sha():
    uses = re.findall(r'uses:\s*(\S+)', workflow_text())
    assert uses, 'workflow must use actions'
    for ref in uses:
        assert re.fullmatch(r'[\w.-]+/[\w.-]+@[0-9a-f]{40}', ref), ref


def test_verify_job_is_read_only():
    text = workflow_text()
    top = text.split('jobs:', 1)[1].split('  verify:', 1)[0]
    assert re.search(r'permissions:\s*\n\s*contents: read\s*\n', text)
    assert 'uv sync --locked' in text
    assert 'uv run --locked python -m pytest' in text


def test_delivery_is_gated_by_tag_or_explicit_dispatch_in_release_environment():
    block = deliver_block()
    assert 'needs: [verify, security]' in block
    assert "(github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v'))" in block
    assert "(github.event_name == 'workflow_dispatch' && inputs.publish_release)" in block
    assert 'environment: release' in block
    # Only version tags (and main) trigger pushes; pull requests never deliver.
    on = workflow_text().split('permissions:', 1)[0]
    assert "tags: ['v*']" in on


def test_security_job_audits_locked_deps_workflows_and_history():
    text = workflow_text()
    security = text.split('  security:', 1)[1].split('  deliver:', 1)[0]
    assert '--extra dense' in security and 'pip-audit==' in security
    assert 'zizmor==' in security
    assert 'GITLEAKS_SHA256:' in security and 'sha256sum -c -' in security
    assert 'fetch-depth: 0' in security


def test_release_build_jobs_restore_no_caches():
    text = workflow_text()
    assert 'enable-cache: true' not in text
    assert 'package-manager-cache: false' in text


def test_delivery_least_privilege_contents_write_only():
    block = deliver_block()
    perms = block.split('permissions:', 1)[1].split('runs-on:', 1)[0]
    entries = re.findall(r'\s{4}([\w-]+):', perms)
    assert entries == ['contents']


def test_delivery_checks_out_exact_commit_without_persisted_credentials():
    block = deliver_block()
    assert 'persist-credentials: false' in block
    assert 'ref: ${{ github.sha }}' in block
    assert 'name: verified-${{ github.sha }}' in block


def test_delivery_runs_the_tested_guard_script_with_explicit_env():
    block = deliver_block()
    assert 'python3 scripts/release_guard.py --root verified' in block
    for var in ('GH_TOKEN: ${{ github.token }}', 'GH_REPO: ${{ github.repository }}',
                'SOURCE_SHA: ${{ github.sha }}',
                "RELEASE_TAG: ${{ github.event_name == 'push' && github.ref_name || inputs.release_tag }}"):
        assert var in block, var
    # No inline guard copy: the script is the single tested implementation.
    assert 'python3 -<<' not in workflow_text()
    assert 'python3 - <<' not in workflow_text()


def test_guard_script_exists_and_matches_dist_name():
    script = REPO / 'scripts' / 'release_guard.py'
    assert script.is_file()
    pyproject = (REPO / 'pyproject.toml').read_text()
    m = re.search(r'^name = "([^"]+)"', pyproject, re.M)
    assert m and m.group(1) == 'quoth'
    assert f"PACKAGE = 'quoth'" in script.read_text()


def test_console_script_compatibility_retained():
    pyproject = (REPO / 'pyproject.toml').read_text()
    scripts = pyproject.split('[project.scripts]', 1)[1].split('[', 1)[0]
    assert 'quoth = "quoth.cli:main"' in scripts
    assert 'quoth = "quoth.cli:main"' in scripts


def test_runtime_version_matches_distribution_version():
    import re as _re
    from quoth import __version__
    pyproject = (REPO / 'pyproject.toml').read_text()
    assert _re.search(r'^version = "([^"]+)"', pyproject, _re.M).group(1) == __version__


def test_concurrency_bounds_overlapping_runs():
    text = workflow_text()
    assert 'concurrency:' in text
    # Only PR runs are superseded; main, tag and dispatch runs always finish.
    assert 'cancel-in-progress: ${{ github.event_name == \'pull_request\' }}' in text
