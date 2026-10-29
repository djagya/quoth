# Verification and release delivery

The repository holds code and synthetic fixtures only: no personal corpus, snapshot
database, private eval data or model cache.

## CI

`.github/workflows/verify-release.yml` runs on pull requests, pushes to `main`, `v*` tag
pushes and manual dispatch. Actions are pinned to full commit SHAs. Python 3.13, Node 22,
uv 0.12.3 and `uv.lock` form the verification environment. The workflow has read-only
repository permissions and restores no caches.

**`verify`**:

1. `uv sync --locked --python 3.13`, the pytest suite, and the synthetic WebUI DOM harness
   (`node tests/playground-dom.cjs`);
2. builds the wheel and sdist, and exports `dist/quoth-<ver>-requirements.txt` (locked,
   hashed, `dense` extra included);
3. installs the wheel into a separate environment from exactly that file with
   `--require-hashes`, and runs `scripts/ci_smoke.py --require-wheel` against it;
4. writes `artifacts/source-commit.txt` and `artifacts/SHA256SUMS` and uploads the packages
   and artifacts.

The smoke test exercises the installed CLI, full and scoped bundle export, validation and
import (row-level parity, a physically excluded sentinel, integrity, an unchanged source),
the installed MCP server (one tool, English and Russian queries with provenance, a call to a
write tool refused, the database unchanged), the built file inventory (no databases,
receipts or virtualenvs), the public eval fixture built and scored from the installed package
(`eval/public/set.jsonl`, vault hit@10 at least 0.8 with the synthetic `hash-ngram` model),
and the packaged plugin skills, WebUI extension, `quoth ledger health` and an empty
`quoth ledger nudge`. The fixture checks plumbing, not real BGE-M3 retrieval quality. Two
WebUI proxy contract tests skip in a plain checkout because their external WebUI source
is absent; the skips stay visible.

**`security`**, on every trigger:

- `pip-audit` over the locked runtime requirements including the `dense` extra (any known
  vulnerability fails);
- `zizmor` static analysis of the workflows (medium severity and above fails);
- `gitleaks` over the full git history, from a release binary pinned by SHA-256
  (configuration: `.gitleaks.toml`).

Dependabot (`.github/dependabot.yml`) opens weekly grouped pull requests for `uv.lock` and
the pinned actions, after a 7-day cooldown on new upstream versions.

### Local replay

```sh
uv sync --locked --python 3.13
uv run python -m pytest -q
node tests/playground-dom.cjs
uv build
uv export --locked --no-emit-project --extra dense --output-file requirements.txt
uv venv --python 3.13 wheel-env
uv pip sync --require-hashes --python wheel-env/bin/python requirements.txt
uv pip install --no-deps --python wheel-env/bin/python dist/*.whl
wheel-env/bin/python scripts/ci_smoke.py --require-wheel --output smoke.json
```

Keep generated environments, requirement exports, reports and temporary databases outside
tracked paths.

## Release delivery

Pull request and `main` runs never publish. Publication starts either by pushing a `vX.Y.Z`
tag (bump `version` in `pyproject.toml` on `main`, then tag that commit) or by a manual
dispatch with `publish_release=true` and an existing tag that points at exactly the
dispatched commit. Either way the `deliver` job needs `verify` and `security` green and runs
in the `release` environment, whose deployment policy admits only `v*` tag refs. The tag push
is the human gate: protect `v*` tags against update and deletion, and require pull requests
with green checks on `main`.

Only the `deliver` job gets `contents: write`. It checks out the exact commit
(`persist-credentials: false`) and runs `scripts/release_guard.py`, the single fail-closed
guard (tested hermetically in `tests/test_release_guard.py`). Before any change it verifies:

- the tag exists and points at the tested commit (it never creates, moves or deletes tags;
  publication uses `--verify-tag`);
- tag version, wheel and sdist file names, and the in-package metadata agree (PEP 440
  `X.Y.Z` or `X.Y.Z{a,b,rc}N`; other forms are refused);
- `artifacts/source-commit.txt` equals the tested commit, and `SHA256SUMS` covers exactly the
  three dist files and matches their digests;
- `quoth-<ver>-requirements.txt` carries the same version and pins every requirement by
  exact version and SHA-256 (no editable, path, VCS, direct-URL or option lines, and not
  quoth itself).

It then publishes the wheel, the sdist and the requirements file once, and reads the release
back by asset name, size, served digest and re-downloaded bytes. Any failure or ambiguity
exits non-zero with a reason: no retry, no overwrite, no destructive cleanup. The guard talks
to GitHub only through `GH_TOKEN`/`GH_REPO` and never prints file contents or secrets.

```sh
uv run python -m pytest tests/test_release_guard.py tests/test_delivery_guards.py -q
```

The workflow publishes packages only. It changes no serving pointer and deploys nothing;
installing a release on a host is described in [`deploy/README.md`](../deploy/README.md).
