# Contributing to quoth

Thanks for helping. quoth holds people's private memory, so the bar is correctness and
privacy first, features second.

## Set up

```bash
uv sync
uv run pytest -q
```

Python 3.13+ and [`uv`](https://docs.astral.sh/uv/) are required. Tests use the synthetic
`hash-ngram` embedder, so no model files are needed. Tests that need the real models
skip without them; run the full suite with real models in Docker (README: "Real models in
Docker"). [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
explains how the pieces fit together.

## Before you open a pull request

- Run the full test suite locally and note the result in the PR description.
- Add or update tests for the behaviour you change.
- Keep one change per pull request, and describe what it changes and why.
- Bump `version` in `pyproject.toml` when the change ships in the wheel, and add a line to
  [`CHANGELOG.md`](CHANGELOG.md).

## Commit messages

quoth uses [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/). `main` is
squash-merge only and the squash commit takes the **pull request title**, so the title is
what must follow the format; CI checks it (`conventional-title`) and merging needs it green.
Write your branch commits the same way.

```
<type>[(scope)][!]: <description>
```

| Type | Use for |
|---|---|
| `feat` | a new capability users can see (CLI, MCP, sidecar, plugin, WebUI) |
| `fix` | a bug fix |
| `perf` | faster or leaner, same behaviour |
| `refactor` | code change with no behaviour change |
| `docs` | documentation only |
| `test` | tests only |
| `build` | packaging, dependencies, the Docker image |
| `ci` | GitHub workflows and Dependabot |
| `chore`, `style`, `revert` | maintenance, formatting, reverting a commit |

- Scope is optional and names the area: `sidecar`, `ledger`, `mcp`, `cli`, `evals`, `plugin`,
  `webui`, `snapshot`, `deps`.
- Description: imperative, lower-case start, no trailing period, at most 100 characters in all.
- A breaking change gets `!` after the type or scope (`feat(cli)!: …`) and a
  `BREAKING CHANGE:` paragraph in the body (the PR description becomes the commit body).

Examples: `feat(models): verify every download against its pinned sha256`,
`fix(sidecar): reject a negative Content-Length`, `build(deps): bump numpy from 2.3 to 2.4`.

## Rules that keep snapshots valid

- **Index-contract files.** Editing a file listed in `CODE_FILES` in `src/quoth/refresh.py`
  (`chunker.py`, `db.py`, `dense.py`, `embed/backends.py`, `embed/store.py`, `entities.py`,
  `ingest.py`, `links.py`, `scopes.py`, `scopes.toml`, `search.py`, `vault.py`), `refresh.py` or `freshness.py`, or
  any evidence-DB migration, changes the code digests, and every existing snapshot becomes
  incompatible until it is rebuilt (`search.py` alone is serving-only and exempt). Put
  serving-speed changes in the serving modules instead, and call out any index-contract
  change in the PR. See [`docs/FRESHNESS.md`](docs/FRESHNESS.md).
- **Migrations are additive.** Add a new numbered SQL file in `src/quoth/migrations/` (the
  evidence database) or `src/quoth/ledger/migrations/` (the ledger); never edit or delete an
  existing one.
- **Never delete memory.** Forgetting is `valid_to` / `superseded_by`, not `DELETE`.
- **Read-only serving stays read-only.** `quoth mcp` must expose only `evidence_bundle`.
- **Public surfaces.** The CLI, the MCP tool, the sidecar's `/v1` API and `quoth.evals` are
  the public API. Document changes to them in `docs/`.

## Privacy

Never commit a personal corpus, snapshot database, private eval data, chat history or model
cache. Everything in `eval/` must be synthetic. CI runs a secret scan (`.gitleaks.toml`).

## Reporting security issues

See [`SECURITY.md`](SECURITY.md). Don't open a public issue for a vulnerability.
