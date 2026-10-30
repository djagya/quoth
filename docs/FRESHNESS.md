# Snapshots, freshness and the nightly refresh

The serving path reads an immutable snapshot, not a live index. This document covers how a
snapshot is built, how to tell whether it still matches the vault, and how the nightly job
replaces it.

## Snapshots and the index contract

A frozen snapshot is a SQLite database plus a sibling `<name>.manifest.json`, both mode 0444
and never written again. The manifest records:

- the database's SHA-256 and size, `status: frozen_current_vault_candidate`, integrity and
  schema checks, the vault root, and `built_at` (UTC, ISO-8601, when the build froze: the
  "vault indexed as of" time);
- `ignore_rules_sha256`: the SHA-256 of the vault's `.quothignore` at build time (`null`
  when there is none);
- the model (`BAAI/bge-m3`) and the SHA-256 of `eval/models-manifest.json`;
- `code_digests`: SHA-256 of every file that shapes the stored data (`chunker.py`, `db.py`,
  `dense.py`, `embed/backends.py`, `embed/store.py`, `entities.py`, `ingest.py`, `links.py`,
  `scopes.py`, `scopes.toml`, `search.py`, `vault.py`) and of every evidence-DB migration;
  `index_code_digests` (the same minus `search.py`, plus `refresh.py` and `freshness.py`);
  `serving_code_digests` (`search.py`);
- counts and an inventory digest of the current note paths and hashes.

A snapshot built by different index code is **incompatible**: its stored chunks or vectors
may not match what the current code would produce, so it is never silently reused. Only
`search.py` (serving only) may differ. Two explicit flags widen this for one build:

| Flag | Allows a difference in | Use |
|---|---|---|
| `--allow-scope-contract-change` (`refresh`) | `scopes.py`, `scopes.toml` | you changed the ingest scope rules on purpose |
| `--allow-index-contract-rebase` (`refresh`, `successor`, `refresh-nightly`) | `embed/store.py`, `refresh.py`, `freshness.py` | the change does not alter stored chunks or vectors |

Changes to chunking or the embedder input (`chunker.py`), ingest, exclusion rules
(`vault.py`), links (`links.py`), schema, entities or the embedder are never covered; rebuild instead. Never set either flag in a scheduled job: once
one successor built across the change is serving, normal runs continue without it.

### Rebuilding across an index change

A release that changes the evidence schema cannot open older snapshots at all, so its
`refresh-nightly` fails (`FAIL`) until the pointer is moved. Rebuild before switching:

1. With the new release, build a fresh snapshot of the same vault path with `quoth snapshot`
   (below) while the old release keeps serving; it only reads the vault.
2. Compare the two on your own set: `quoth eval run SET --db OLD --model bge-m3 --gold-drift
   --out old.json` with the old release, the same with `--db NEW` and `--out new.json` with the
   new one, then `quoth eval compare old.json new.json`.
   A fresh snapshot has no version history, so a gold path that names a note renamed before
   the rebuild no longer resolves (`gold_drift.stale` in the new receipt). Update such paths
   in the gate's and the private runner's sets, or the nightly gate fails with
   `gold_drift_excessive`.
3. Point the serving pointer at the new snapshot (an atomic symlink replace) and restart the
   sidecar on the new release before the next `refresh-nightly` run. Keep the old snapshot and
   the old release together as the rollback pair.

## What a snapshot holds

### Exclusion

There is one exclusion path (`vault.py`), used by ingest and by the freshness inventory
alike. A Markdown note is indexed unless:

- a **built-in rule** excludes it: a hidden name (a leading `.`) anywhere on its path, or a
  folder named in `exclude_dirs` in `scopes.toml` (`ASSISTANT`, attachment folders);
- the vault's **`.quothignore`** ignores it or a folder above it;
- its **frontmatter** says `quoth: exclude`.

`.quothignore` sits at the vault root and uses gitignore syntax: `#` comments, `!` negation
(the last matching rule wins), a trailing `/` for folders only, `*`, `?`, `[...]`, `**`, and
anchoring (a pattern with a `/` anywhere but at its end matches from the vault root; one
without matches at any depth). As in git, a note inside an ignored folder cannot be
re-included, and matching is case-sensitive. Its rules add to the built-in ones and cannot
re-include what those exclude.

```gitignore
# keep these out of quoth entirely
Health/
/Journal/*
!/Journal/Reading list.md
*.private.md
```

An excluded note is never chunked, embedded or stored. **Excluded means purged:** when a
note that was indexed becomes excluded, the next build deletes all of its rows, current and
historical (source, versions, chunks, vectors, entities, lexical-index rows and links), merges
the lexical indexes, and the freeze `VACUUM` rewrites the file, so its text is not left in
free pages either (`ingest.purged` in the manifest counts these notes). A note that is merely
deleted from the vault keeps its retired history, as before. A snapshot that holds any row
under a built-in exclusion is `invalid`.

**Rollback re-exposes excluded material.** An older snapshot still holds whatever it held
when it was built. Pointing serving back at it brings back notes you have since excluded;
delete older snapshots (and bundles made from them) that hold such notes.

Changing `.quothignore` (adding, editing or removing it) makes the snapshot `stale`
(`ignore_rules_changed`), even when no indexed note changes; the successor records the new
digest.

### Links

The `links` table holds every link from a current chunk to another note: wikilinks
`[[Target]]`, `[[Target|alias]]`, `[[Target#Heading]]`, embeds `![[...]]` and relative
Markdown links `[text](path.md)` (frontmatter, fenced and inline code excluded).

| Column | Meaning |
|---|---|
| `chunk_id` | the linking chunk (a link in an overlap appears in both chunks) |
| `target` | the target as written, without `#heading` and alias |
| `heading` | the `#heading` (or `#^block`) part, or NULL |
| `line` | 1-based line in the linking note |
| `kind` | `wikilink`, `embed` or `markdown` |
| `status` | `resolved`, `missing`, `ambiguous` or `excluded` |
| `target_path` | the vault-relative path of the target note; set only when `resolved` |

Resolution follows Obsidian: an exact vault path (with or without `.md`, relative to the
linking note for `./`, `../` and Markdown links; case-insensitive when unique), else a unique
match on the path's tail (`[[Note]]`, `[[Sub/Note]]`). Several matches give `ambiguous`; a
target that is an excluded note gives `excluded`, and its path is never stored (excluded
notes still take part in matching, so a link to one never resolves to another note of the
same name). Every current link is resolved again on each build, so links follow notes as
they are added, renamed or removed. In a scoped bundle, a link to a note outside the scope
becomes `excluded` too.

### Facts for "changed since indexing"

Each current version (`source_versions`, with `sources.path`) records `content_hash` (the
SHA-256 of the file bytes that were indexed), `mtime_ns` and `size_bytes` (the file's stat
when it was read, kept current while the content is unchanged). A reader can compare them
with the live file to say a note changed after the snapshot was built.

### Recall tiers

Each scope declared in `scopes.toml` has `recall = "auto"` (the default) or `"explicit"`.
Both are indexed the same way; the tier only tells the serving side whether the scope's notes
may appear in automatic recall (`quoth.scopes.recall_tier(scope)`,
`quoth.scopes.explicit_scopes()`). The package ships `general` and `work` as `auto` and
`private` as `explicit`; a `[[rule]]` maps folders to a scope.

## Building a snapshot: `quoth refresh`

Build the first snapshot from scratch with `quoth snapshot`. It ingests and embeds the vault
into a new file and writes the same manifest a refresh writes (with `source_db_sha256: null`),
so the result can be served and used as the source of every later refresh. A killed build
leaves its checkpoint and every committed batch: rerun with the same `--target`, `--vault`
and `--resume` to embed only what is missing. The checkpoint must name this target and vault
and match the current code and model digests; otherwise start a new target:

```sh
quoth snapshot --vault /abs/vault --target /abs/snapshots/first.db
```

| Flag | Default | Meaning |
|---|---|---|
| `--target PATH` | required | the new snapshot; must not exist yet |
| `--vault DIR` | required | the vault to index |
| `--resume` | off | continue this exact interrupted build from `<target>.checkpoint.json` |
| `--retries N` | 2 | embedding retries when the vault changes during the build |
| `--threads N`, `--batch-size N` | 4, 32 | embedding settings |

Every later snapshot comes from `quoth refresh`:

```sh
quoth refresh --source /abs/accepted.db --target /abs/staging/unique-name.db --vault /abs/vault
```

| Flag | Default | Meaning |
|---|---|---|
| `--source PATH` | required | a frozen snapshot with its manifest; never modified |
| `--target PATH` | required | a new, unique output; an existing frozen snapshot is never overwritten |
| `--vault DIR` | required | the vault the source was built from (must match the manifest's vault root) |
| `--resume` | off | continue this exact unfinished target from its checkpoint |
| `--retries N` | 2 | embedding retries when the vault changes during the build |
| `--threads N`, `--batch-size N` | 4, 32 | embedding settings |

It copies the source with SQLite's backup API, validates the source manifest, digests, model
manifest and inventory, ingests the vault, embeds what is new, retries within the budget if
the vault drifts mid-build, then checks the target (path and hash inventory equal to the vault,
full current BGE-M3 coverage, integrity, read-only open) and freezes it. It prints the receipt
as JSON.

- **Vector reuse.** A chunk whose exact embedder input (note title and aliases, heading path
  and text) already has a vector, from any version, reuses it. A changed note costs only its
  changed chunks (`embed.reused` in the manifest).
- **Embedding.** Every chunk is read whole (BGE-M3 takes up to 8192 tokens). Pending chunks
  run shortest first, in batches of similar length that hold at most `--batch-size` chunks and
  at most the attention work of `--batch-size` chunks of 512 tokens, so a long chunk never
  raises peak memory.
- **Retired vectors.** Vectors of non-current versions are deleted and the database is
  vacuumed before freezing (`pruned_retired_vectors`). Sources, versions and chunks are never
  deleted, except those of a note that became excluded (above).
- **Resume.** After an interruption, rerun with the same `--source`, `--target`, `--vault` and
  `--resume`. An interruption before the target database exists cannot be resumed: pick a new
  target.

The source must itself be a frozen snapshot with a full manifest. quoth does not yet have a
command that freezes a development database (`quoth ingest` + `quoth embed`) into the first
such source.

## Checking freshness: `quoth freshness`

```sh
quoth freshness --source /abs/snapshot.db --vault /abs/vault
```

Prints one sorted JSON object (no timestamps, note paths, snippets or exception text) and
writes nothing. Repeated checks against unchanged inputs give byte-identical output.

| State | Exit | Meaning |
|---|---|---|
| `fresh` | 0 | the vault's current allowed paths and hashes equal the snapshot's |
| `stale` | 3 | a valid, compatible snapshot; `delta` gives added, removed and changed counts; reasons `inventory_changed` and/or `ignore_rules_changed` |
| `incompatible` | 4 | code, scope, model or vault contract differs; no automatic migration |
| `invalid` | 5 | unreadable or corrupt snapshot, incomplete vectors, any row (current or historical) under a built-in exclusion such as `ASSISTANT/`, inconsistent manifest, or vault drift during the check |

Exit 1 is a runtime failure and 2 a usage error. The checks cover the database digest and
size, schema, integrity, foreign keys, `query_only`, manifest counts and inventory digest, full
current BGE-M3 coverage, and no source, current or historical, under a built-in exclusion. The model
manifest is compared as a contract; model files are not loaded. A check hashes the whole
database and scans the vault twice, so do not poll it at high frequency.

## Building a successor: `quoth successor`

```sh
quoth successor --source /abs/accepted.db --vault /abs/vault --target /abs/staging/unique-name.db
# after an interruption:
quoth successor --source /abs/accepted.db --vault /abs/vault --target /abs/staging/unique-name.db --resume
```

The target path is the operation's identity: keep it across retries and do not run `quoth
refresh` against it at the same time.

- A fresh source returns `action: none` and creates nothing. An incompatible or invalid
  source also produces no candidate.
- A stale source runs the refresh engine (checkpoint, vector reuse, drift retries, freeze),
  then checks that the candidate is fresh and descends from the source. The result is
  `action: activation_required` with the candidate, the rollback source and the candidate's
  freshness report. The exit code stays 3, because the accepted source is still stale.
- A retained `<target>.lock` serializes cooperating calls; a busy call fails at once
  (`candidate_busy`). Do not delete the lock while a builder might run.
- A frozen target that has gone stale is refused: choose a new target.

No configuration is changed and nothing is switched. To activate the candidate, recheck its
freshness, point the serving pointer (or your MCP config) at it, and verify the MCP tool.

## Nightly refresh

`quoth refresh-nightly` automates build, gate and switch around a **serving pointer**: a
symlink to the snapshot being served. Both `quoth mcp --db POINTER` and `quoth sidecar --db
POINTER` follow it without a restart: a watcher thread polls the pointer every few seconds,
validates a new target (manifest, sha256, `integrity_check`) and warms it off the request
path while the previous snapshot keeps serving, then swaps it in. A target that fails is
rejected and not validated again until the pointer or the file changes, or 15 minutes pass
(then 30, then hourly); the sidecar's `/v1/health` reports it as `pointer_rejected` (red)
with a digest of the target, and a sidecar started on a bad pointer serves without evidence
until the pointer is fixed. Requests never validate. See
[HERMES.md](HERMES.md#snapshot-admission).

```sh
quoth refresh-nightly --pointer "$QUOTH_HOME/staging/serving.db" --vault /abs/vault \
  --staging "$QUOTH_HOME/staging" --receipts "$QUOTH_HOME/refresh/receipts" \
  --gate-set /private/gate.evalset.jsonl --private-runner /private/runner.py \
  [--quality-set /private/quality.evalset.jsonl]
```

| Flag | Default | Meaning |
|---|---|---|
| `--pointer PATH` | required | symlink to the serving snapshot |
| `--vault DIR` | required | the vault |
| `--staging DIR` | required | where successors and their checkpoints are built |
| `--receipts DIR` | required | run receipts (directory 0700, files 0600) |
| `--gate-set SET` | required | eval set v1 the gate scores both snapshots on; write it against your own vault and keep it private |
| `--private-runner PATH` | required | the private gate scorer (below) |
| `--quality-set SET` | off | eval set v1 for the daily serving-quality block |
| `--deadline-seconds S` | 3000 | internal wall-clock budget (> 0, ≤ 3000) |
| `--heartbeat PATH` | `<receipts>/../heartbeat.json` | heartbeat written after every run |
| `--allow-index-contract-rebase` | off | one operator-run build across a declared rebase |

A run:

1. takes a lock next to the pointer (a second run returns `BUSY`);
2. checks freshness: unchanged vault → `SKIP`;
3. requires free space of at least twice the serving database;
4. resumes the newest compatible unfinished `nightly-*` target in staging, or starts a
   unique `nightly-<UTC>-<random>.db`, and builds it with the successor engine;
5. scores both snapshots on the gate (below), rechecks freshness, and switches the pointer
   atomically (`rename`, then `fsync` of the directory) only when the gate passes;
6. opens a fresh stdio MCP client on the pointer and checks the snapshot id and the
   single-tool registry through a real call; on failure it restores the previous pointer,
   unless someone else changed it meanwhile. The `quoth mcp` child gets the run's `QUOTH_*`
   variables and `HERMES_HOME` (the SDK passes only `PATH`, `HOME` and a few others), and a
   failed call is recorded as `self_check_error: {code, class}` (`probe_tool_error` with the
   exception class, or `probe_not_json`), never with the error's text;
7. after a successful check, deletes older frozen `nightly-*` outputs in staging, keeping the
   current, the previous and the last held candidate;
8. records the day's serving quality, writes the receipt and the heartbeat.

| Outcome | Meaning | Prints | Exit |
|---|---|---|---|
| `SWITCH` | successor passed and is now served | one line | 0 if the heartbeat is green |
| `SKIP` | vault unchanged | nothing | 0 if the heartbeat is green |
| `HOLD` | successor failed the gate; serving unchanged | one line | 3 |
| `INCOMPLETE` | deadline reached; checkpoint kept for the next run | one line | 3 |
| `FAIL` | a step failed; serving unchanged or rolled back | one line | 1 |
| `BUSY` | another run holds the lock | nothing | 0 |

A non-green heartbeat also exits 3 and prints `RED quoth-heartbeat <reasons>`. Output never
contains questions, note paths or text.

### The gate

The gate compares the successor with the serving snapshot on two datasets: `synthetic` (the
`--gate-set`) and `private` (the runner's set). It runs in one of two modes, recorded as
`gate_mode`:

| Mode | When | Rule |
|---|---|---|
| `content` | both manifests carry identical index-code digests (or differ only in the rebase files under `--allow-index-contract-rebase`): only vault content changed | private hit@10, private MRR@10 and synthetic recall@10 may each drop by up to 0.03; an item that loses all its gold is excused only when a gold note was changed (`gold_note_changed`) or deleted (`gold_note_removed`) by this refresh |
| `code` | anything else | no private hit@10 or MRR@10 drop, synthetic recall@10 drop at most 0.01, no item may lose all its gold |

A pure rename is not a content change: gold drift follows it, so a loss there still blocks.
Receipts list `lost_gold_ids` and `excused_lost_gold` (`{id, reason}`) per dataset; gold paths
are used in memory only. The thresholds are `GATE`, `CONTENT_TOLERANCE` and `CONTENT_GATE` in
`nightly.py`. Scoring details: [EVAL.md](EVAL.md#the-nightly-gate-and-daily-quality).

### The private gate runner

`--private-runner PATH` names a Python file that is executed (never imported, so no
`__pycache__` is written beside it). It must define:

```python
def validate(rows):            # raise if the private rows are not what you expect
    ...

def metrics(hits_top5, hits_top10, expected_paths):
    # hits: evidence hits (dicts with "path"); expected_paths: a set of note paths
    return {"hit@10": 0 or 1, "mrr@10": float}
```

Its rows are read from `real-questions-v2.jsonl` in the same directory: JSONL with `id`,
`question` and `expected_paths` (and optional `kind`). Rows without expected paths, or of
kind `adversarial`/`unanswerable`, are not scored.

### Heartbeat: `quoth heartbeat`

```sh
quoth heartbeat --pointer POINTER --receipts DIR [--output FILE] [--max-age-hours 36]
```

Prints one JSON object (`schema: quoth-heartbeat/v1`) and exits 0 green, 1 red.
`refresh-nightly` writes the same object after every run that is not `BUSY` (atomic replace,
mode 0644).

Fields: `status` (`green` or `red`), `reasons`, `checked_at`, `fresh_as_of` (the newest
`SWITCH` or `SKIP` receipt: when serving was last confirmed to match the vault),
`fresh_age_hours`, `max_fresh_age_hours`, `max_run_age_hours`, `serving` (`db`, `frozen_at`,
`age_hours`), `last_run` (`receipt_id`, `outcome`, `reasons`, `recorded_at`, `age_hours`).

| Reason | Red when |
|---|---|
| `freshness_unconfirmed` | no `SWITCH` or `SKIP` within 36 hours, whatever the last outcome |
| `last_run_hold`, `last_run_incomplete`, `last_run_fail` | the newest receipt has that outcome |
| `last_run_overdue` | the newest receipt is older than 26 hours (the job did not run) |
| `serving_pointer_invalid`, `no_receipts` | as named |

A monitor should alarm on `status != "green"` and on the heartbeat file itself being old;
`quoth health` does both (see [the README](../README.md#health)).

### Daily serving quality

The first run per served snapshot per UTC day also scores the served snapshot (the private
set, the gate set and, with `--quality-set`, the `golden_v3` block: hit@5, hit@10, MRR@10,
recall@10, the abstain rows' non-empty count, latency p50/p95). Later runs that day reuse the
result while the code, inputs and vault inventory are unchanged (`cached`). A quality failure
is recorded as unavailable; it never changes the switch decision. The block lives in the
nightly receipt under `serving_quality` (`quoth-serving-quality/v1`).

### Limits

- The pointer lock coordinates `refresh-nightly` runs, not arbitrary other writers of the
  pointer.
- Pointer targets must be unique, immutable files; replacing a database in place under the
  same name is unsupported.
- The deadline is checked at interpreter boundaries: a long native operation can overrun it,
  which is why the default leaves headroom under a one-hour scheduler cap.
- Retrieval metrics measure whether the expected notes are found, not answer correctness.
