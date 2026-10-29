# Portable bundles

A **bundle** is a portable, versioned, immutable snapshot of a memory
database: `<name>.db` plus a `<name>.manifest.json` beside it. Full bundles
carry the whole logical corpus; **scoped** bundles (for example only the notes labeled
`work` in `scopes.toml`) physically omit every row that is not in the declared scope's
closure. Every command prints its result as JSON.

## Commands

```bash
# Freeze a full bundle (the source is opened read-only).
quoth bundle export --source /path/staging.db --target /path/bundles/full-1.db

# Freeze a scoped bundle (only the notes labeled scope=work).
quoth bundle export --source /path/staging.db --target /path/bundles/work-1.db --scope work

# Verify a bundle (format, digests, schema, inventory, scope closure).
quoth bundle validate --db /path/bundles/work-1.db

# Restore a bundle into a fresh, usable local database.
quoth bundle import --db /path/bundles/work-1.db --target /path/restored.db
```

| Command | Flags |
|---|---|
| `bundle export` | `--source PATH`, `--target PATH` (new; the manifest is written beside it), `--scope LABEL` (default: full), `--resume` (restart this exact unfinished export) |
| `bundle validate` | `--db PATH`, `--manifest PATH` (default: beside the database) |
| `bundle import` | `--db PATH`, `--target PATH` (new local database), `--manifest PATH` |

## How the invariants are held

- **Consistent capture, never a raw copy.** The source is opened read-only
  (`query_only`) and captured with the SQLite online-backup API into a
  private staging snapshot. A concurrently active writer cannot tear the
  image, and committed-but-WAL-resident rows are captured; the source file
  is never written, checkpointed, or copied bytewise.
- **Physical exclusion.** The bundle database is built fresh (with the current
  evidence schema), and only selected rows are ever `INSERT`ed into it. Excluded
  content never enters the file, so scoped bundles cannot leak it through
  leftover pages; the finished file is additionally `VACUUM`ed. Derived FTS
  indexes are rebuilt from the selected current chunks only.
- **Scope closure.** `sources(scope = S)` → their `source_versions` (all
  history) → `chunks` → `entities` / `embeddings` / FTS. The kept and
  eligible row counts of every table are recorded in the manifest
  (`selection`).
- **Private operation-owned staging.** Everything an export touches before
  publication lives in one sibling `<name>.stage` directory, created `0700`
  with `0600` files regardless of umask, and identified by an `owner.json`
  record (pid, thread, exact source/target/scope). The full-corpus capture
  never leaves staging and is never promoted into a deliverable. Nothing
  outside staging is created, unlinked or replaced: a foreign file, symlink
  or unknown staging entry is refused intact, never repaired.
- **Atomic completion.** The bundle db and the manifest are each serialized
  and fsynced inside staging first, then published by hard link without
  overwrite (`link(2)` fails atomically if the name exists in any form,
  including dangling symlinks), with the containing directory fsynced. A
  manifest found at its final path is therefore always a complete document;
  its existence marks the bundle complete. A crash mid-serialization leaves
  nothing at any published name.
- **Bounded recovery.** An interrupted export leaves its private staging
  plus a checkpoint recorded inside it. Re-running with `--resume` verifies
  the owner record and the captured-source digest and rebuilds
  deterministically: resuming against a drifted source fails closed, a live
  owner (another process or thread) is never displaced, and a db already
  published by that staging is adopted — a foreign file at the target name
  is refused, not overwritten. Two exports racing for one target collide at
  staging creation and the loser deletes nothing.
- **Validation is logical, not just bytewise.** Besides the db
  sha256/bytes, `validate` recomputes a canonical per-table row digest
  (`corpus.digest`) ordered by primary key, so import parity is checked at
  row level even though page layouts differ. Integrity check, foreign-key
  check, FTS-vs-chunks parity, scope uniformity, per-table counts, and
  embedding model inventory are all verified.

## Manifest fields

| Field | Meaning |
| --- | --- |
| `format` / `compatibility` | `quoth-bundle/1`; readers reject unknown formats |
| `bundle_id` | digest of {format, kind, scope, db, corpus, schema} identity |
| `kind`, `scope` | `full` or `scoped` + the scope label |
| `status`, `errors` | `complete` / `[]` — manifest exists only for clean completions |
| `source` | capture mechanism (`sqlite_backup`), snapshot digest/size, per-table counts |
| `selection` | per-table kept/eligible (scoped) |
| `tables`, `derived` | per-table count and state: included / rebuilt from current chunks |
| `schema` | exact `schema_migrations` identity + digest; import requires an exact match |
| `engine` | SQLite version that produced the bundle |
| `models` | `(model, dim, vectors)` inventory of the `embeddings` table |
| `pipeline` | digest of `bundle.py` + the ingestion-contract code digests |
| `corpus` | canonical logical row digests per table |
| `db` | bytes + sha256 of the bundle database |

## Limits and non-goals

- Bundles are frozen artifacts: export targets are unique, chmod `0444`,
  and never overwritten (publication is a no-overwrite link; imports restore
  into fresh `0600` targets and a failed import removes only the partial
  target it created).
- Interrupted exports are recoverable only through `--resume` of the same
  source/target/parameters; manual cleanup of a `<name>.stage` directory is
  safe when no export for that target is running.
- Import compatibility is strict on format and schema, informational on
  pipeline/engine drift (`compatibility_drift` in the import report).
- Dense retrieval over an imported database still needs the embedding model
  at query time; the vectors travel inside the bundle, the model does not.
- Scope labels are free-form; an unknown label that matches no sources fails closed
  instead of producing an empty bundle.
- The test suite exercises bundles on synthetic data only.
