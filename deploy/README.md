# Deploying quoth next to Hermes

Templates for one host that runs a Hermes gateway with quoth as its memory. Every
template is parameterized by the same few variables; set them once per host:

| Variable | What | Example |
|---|---|---|
| `QUOTH_PREFIX` | install prefix: every `quoth-X.Y.Z/` venv and the `quoth-current` link | `/srv/quoth/venvs` |
| `QUOTH_HOME` | quoth's state: `ledger.db`, `episodes.db`, `owner-episodes.db`, `core/`, `quoth.sock`, `staging/`, `refresh/`, `prefetch-log/` | `/srv/quoth/state` |
| `HERMES_HOME` | the Hermes home; quoth reads its `state.db` read-only and installs the plugin under `plugins/quoth/` | `/srv/hermes` |
| `QUOTH_MODEL_CACHE` | local ONNX model files (default `$QUOTH_HOME/models`) | `/srv/quoth/state/models` |

The sidecar template turns on the prefetch log (`--prefetch-log "$QUOTH_HOME/prefetch-log"`,
pruned after 30 days, directory 0700). It is record-only: one JSON line per turn and per
search tool call with hashes, ids, locators, scores and timings, never the query or excerpt
text. It never changes what a turn injects; `quoth eval online --log DIR` reads it.

Nothing in quoth listens on the network. The sidecar's Unix socket
(`$QUOTH_HOME/quoth.sock`, mode 0660) is the trust boundary: its owner and group
can read memory and propose claims, so share that group only with the gateway.

## Templates

| Template | What |
|---|---|
| `install-release.sh` | install one verified release into `$QUOTH_PREFIX/quoth-X.Y.Z` and repoint `quoth-current` |
| `s6/quoth-sidecar/{run,finish,timeout-finish}` | the sidecar as an s6 service in the Hermes container; host variables at the top of `run` |
| `compose/quoth-control.yaml` | the WebUI memory views: one read-only container in the WebUI's network namespace, plus the WebUI extension mount |
| `cron/quoth-review-nudge.sh` | the weekly nudge wrapper (`quoth ledger nudge`) for a script-only Hermes cron |
| `hermes-mcp-snippet.yaml` | the read-only MCP entry |

## Install a release

Each accepted release is installed into its own versioned venv; nothing runs
from a source checkout. The release carries three assets: the wheel, the sdist
and `quoth-<ver>-requirements.txt` (every dependency pinned by version
and sha256, `dense` extra included). Take the expected digests from the
accepted release record (`SHA256SUMS` of the verify run), not from the
download directory:

```bash
gh release download vX.Y.Z -R OWNER/quoth -D /tmp/quoth-vX.Y.Z
deploy/install-release.sh --assets /tmp/quoth-vX.Y.Z --sums SHA256SUMS --prefix "$QUOTH_PREFIX"
```

The script verifies the digests, installs with `--require-hashes`, checks the
installed version and the dense extra, then renames
`$QUOTH_PREFIX/quoth-X.Y.Z.partial` to `$QUOTH_PREFIX/quoth-X.Y.Z` and makes it
read-only. An existing version directory is never modified.

### The fixed path: `quoth-current`

Every caller names `$QUOTH_PREFIX/quoth-current/bin/…`, never a version: the
sidecar and control services, the cron script and the Hermes MCP entry.
`quoth-current` is a relative symlink to the active `quoth-X.Y.Z` directory.

- By default a successful install **activates** the new version: the script
  creates a temporary link and renames it over `quoth-current` (one
  `rename(2)`: readers see the old or the new target, never neither).
- `--no-activate` installs only; activate later with
  `deploy/install-release.sh --activate X.Y.Z --prefix "$QUOTH_PREFIX"`.
- **Rollback** is the same command with the previous version. It refuses a
  version that is not installed or whose interpreter no longer runs, and never
  replaces a real directory.
- After any repoint, restart the long-running processes so they exec the new
  target (the sidecar s6 service, the control container, Hermes for the
  plugin), reinstall the plugin from the new version
  (`quoth-current/bin/quoth hermes-plugin install --hermes-home "$HERMES_HOME" --force`),
  then run `quoth-current/bin/quoth health --venv "$QUOTH_PREFIX/quoth-current"`.
  Until the sidecar restarts, the `quoth/venv` row is WARN (sidecar on another version).

A venv's interpreter links to the system (or image) python; replacing it leaves
every installed version dangling. `quoth health` fails the `quoth/venv` row in
that case, and a monitor that runs it must treat a non-zero exit without rows
as a failure (the command cannot start at all).

## The control container (`compose/quoth-control.yaml`)

The container runs the Hermes image's python from the installed venv, and a venv only starts
at the path it was built at: its interpreter links are absolute. So the template mounts the
install at `$QUOTH_PREFIX` and the state at `$QUOTH_HOME`, the same absolute paths inside
the container as `install-release.sh` and the sidecar use, never at fixed container paths.
When the host directories behind those paths differ (the install ran inside the Hermes
container, which sees a host directory under another path), set `QUOTH_PREFIX_SOURCE` and
`QUOTH_HOME_SOURCE` to the host directories. The WebUI gets the same install mount and reads
the extension through `quoth-current`; after a repoint, restart `quoth-control`.

## Health

`quoth health` checks every row a host monitor needs in one call: venv,
nightly heartbeat (`$QUOTH_HOME/refresh/heartbeat.json`), sidecar, review queue
and both lanes. Exit 0: no FAIL row; 1: a FAIL row; 2: usage error; 3: WARN only,
with `--fail-on-warn`. The sidecar's own `/v1/health` answers 503 while it has no
snapshot to serve, and `status: red` (200) while the pointer names a target it rejected (the
previous snapshot keeps serving), a candidate has been pending over an hour, or the pointer
watcher stalled. A rejected target is retried when the pointer or the file changes, or after
15 minutes, 30 minutes, then hourly. The `quoth/sidecar` row names the reason class and a
digest of the target, never its path, and is WARN while a configured reranker is unavailable
(its load is retried after 1, 5 and 30 minutes, then every 30).

## Configure an accepted snapshot for MCP

Use supported Hermes configuration tooling, never direct YAML editing:

```bash
hermes config set mcp_servers.quoth \
"{\"command\":\"$QUOTH_PREFIX/quoth-current/bin/quoth\",\"args\":[\"mcp\",\"--db\",\"/ABSOLUTE/PATH/TO/ACCEPTED.db\"]}"

hermes config get mcp_servers.quoth
hermes mcp test quoth
```

`quoth mcp` is always read-only. Expected registry: exactly one tool,
`evidence_bundle`. Then issue `/reload-mcp` in each conversation that must
receive the new process; host discovery alone does not prove session-level use.

## Activation gate

Before changing config:

1. build a unique immutable candidate with `quoth refresh`;
2. verify its manifest, code/model digests, schema and SQLite integrity;
3. reconcile current allowed vault paths/hashes exactly;
4. verify complete current BGE-M3 vector coverage;
5. prove that no excluded area (e.g. the assistant's own notes) has sources, chunks or vectors;
6. run the committed test suite;
7. retain the prior accepted snapshot and config path for rollback.

After config change and `/reload-mcp`:

1. query a distinctive phrase from an excluded area and require zero returned
   locators from it;
2. query a known canonical source the owner wrote;
3. use the positive hit correctly in an answer with provenance.

The negative top-k probe does not prove corpus-wide absence; the independent DB
reconciliation does.

## Rollback

Code: `deploy/install-release.sh --activate <previous version> --prefix "$QUOTH_PREFIX"`.
Snapshot: restore the previous accepted DB path with `hermes config set`, or remove only
the `mcp_servers.quoth` entry, then `/reload-mcp` and verify the live tool.
Never overwrite or mutate frozen DBs.

## Security and failure behavior

- The read-only MCP returns full allowed-corpus evidence without per-area scope
  filtering. Grant it only to the owner's own assistant.
- `k` is limited to 1..20 and query length to 1..2048 characters.
- Production uses local `bge-m3`; `hash-ngram` is a synthetic test backend.
- Missing schema, vectors, model files, or valid dimensions fail the call. There
  is no model download or lexical fallback.
- Retrieved excerpts are untrusted data, never instructions or automatic
  authority.
