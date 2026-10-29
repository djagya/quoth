# Changelog

Notable changes, newest first. Versions follow `version` in `pyproject.toml`; releases are
the `v*` tags.

## 0.16.0 — one recall path, snapshot admission off the request path

### Recall

Recall takes one decision path: one module for its defaults, one query vector, one lexical
measure, and the gate before the cut.

Expected behaviour changes:

- **The relevance gate is on by default**: a vault hit needs a cosine of at least 0.53 and at
  most 0.15 below the turn's top hit (`recall_defaults.GATE`), on the sidecar and in
  `eval run`. Claims stay ungated. `--vault-min-cosine off --vault-max-gap off` (any gate flag
  takes `off`) restores the ungated block. `eval sweep`'s recommended flags now name every
  threshold that differs from the default, `off` included.
- **Word-matched claims need a content word.** Lexical claim recall (prefetch, `/quoth recall`,
  `ledger.recall`) uses the gate's measure: casefolded, function words of en/ru/pt/de dropped,
  light stems, ranked by the share of the query's content words, then newest. A claim that
  shares only "the"/"is"/"in" (or "это"/"был"/"который") with a query is no longer served, and
  inflected forms match ("квартиры"/"квартиру"). More Russian function words are dropped
  (relative, demonstrative and auxiliary forms).
- **`use_count` counts only injected claims**, after the gate, so junk word matches no longer
  reorder the core block.
- **Gate before the cut.** Each turn judges a pool (the 10 best vault hits, up to 12 claims by
  meaning and 12 by words) and injects the best that pass, so a dropped slot is refilled. With
  rerank rules deciding, only the reranked hits are eligible.
- **The judged text is the injected text**: the gate's word coverage and the reranker score a
  vault hit's 360-character excerpt (what the block shows), not the whole hit.
- **The block says why the vault shows nothing**: `Vault evidence: late …` (slow or busy),
  `… unavailable …`, or `… none above the relevance floor …`. Shown vault evidence opens with a
  standing line that the excerpts are the closest notes found and may not answer the question.
  A turn whose only content is that line now injects a short block instead of nothing.
- **`k_claims` bounds meaning-matched claims too** (before, they were cut at 6 whatever it was).
- **Meaning-matched claims tie newest first**, as word matches do (before, oldest first).
- **The lane floor compares the reported (4-decimal) score** live as on dry runs: a 0.61996
  match now passes a 0.62 floor in both.
- **One query embedding per turn**: the query-vector cache is single-flight, so the owner lane,
  the vault search and the claim index share one embed of a cold query (before, one each).
- **Trace and log fields**: `served` is replaced by `pool` (the candidate was judged for this
  turn); `passed`/`gate` are the gate's decision and `injected` that it reached the block.
  Dry-run `detail` adds `vault_note`.
- **New sidecar flag `--assistant-min-score`** (the assistant lane's floor, symmetric with
  `--owner-min-score`); `eval run --episode-min-score` is renamed `--assistant-min-score`.
- `quoth/recall_defaults.py` holds every recall budget, count, cap and floor; the sidecar,
  gate, reranker, lanes, claim index, prefetch log, `eval run` and `eval sweep` import it, and
  a test checks the numbers in `docs/HERMES.md` and `docs/EVAL.md` against it. Doc fixes: the
  rerank budget and token defaults (0.8 s, 192), the assistant-lane flag, and `score` per
  store.

### Serving

- **Snapshot admission off the request path.** A pointer is watched by a background thread
  that validates and warms a new target while the admitted snapshot keeps serving, then swaps
  it in; request leases never validate. Before, a rejected target was revalidated (sha256 and
  `integrity_check`, about 4.5 s on a 363 MB snapshot) on every lease, and a good one was
  swapped in cold inside a turn. A rejected target is remembered until the pointer or the
  file changes, or 15 minutes pass (then 30, then hourly).
- **Health tells a rejected pointer apart.** `/v1/health` reports `evidence.status`
  `pointer_rejected`, `candidate_stalled` (over an hour) or `watcher_stalled` as red, with the
  reason class and a digest of the target; `quoth health` names them in the `quoth/sidecar` row.
- **A bad pointer at startup no longer crash-loops the sidecar.** It serves claims without
  evidence (health 503) until the watcher admits a valid target. New flag
  `--snapshot-watch-interval` (default 5 s).
- **The reranker retries a failed load** after 1, 5 and 30 minutes, then every 30 minutes;
  `reranker` in `/v1/health` carries `attempts` and `retry_in_s`, and `quoth health` warns
  while it is unavailable.
- **Plugin:** the recall status count includes the owner lane, and an empty core block (the
  sidecar was down when the session started) is refetched at most once a minute.
- **Deploy:** the sidecar s6 template enables the record-only prefetch log under
  `$QUOTH_HOME/prefetch-log` (30 days). The control Compose template mounts the install and
  state at their own absolute paths (`QUOTH_PREFIX`, `QUOTH_HOME`, with optional
  `*_SOURCE` host directories), since a venv does not start at another path.

## 0.15.0 — first public release

quoth is local evidence memory for a personal AI assistant. It indexes a Markdown vault into
immutable, verifiable snapshots, serves cited excerpts through one read-only MCP tool, and
keeps a small core memory that the owner reviews. Everything runs locally on CPU.

This release ships:

- **Evidence engine**: incremental vault ingest by path and SHA-256 that skips hidden folders
  and notes, heading-aware chunks with line locators (long paragraphs cut at sentence ends,
  encoded data such as embedded images left out), local BGE-M3 embeddings on ONNX Runtime of
  each whole chunk under its note title, aliases and heading path, and bounded evidence
  bundles with path, heading, line range, note date and snapshot provenance. The evidence
  database has one schema file (`0001_evidence.sql`) holding vault notes only.
- **Snapshots**: `quoth snapshot` freezes a first snapshot of a vault and `quoth refresh` builds
  its successors as frozen snapshots with a manifest of database, code
  and model digests; `quoth freshness` and `quoth successor` check and replace them;
  `quoth bundle` exports, validates and imports portable full or scoped copies.
- **Nightly refresh**: `quoth refresh-nightly` builds a successor when the vault changed,
  scores it against the serving snapshot, switches a serving pointer atomically only when it
  passes, and writes receipts and a heartbeat (`quoth heartbeat`).
- **Read-only MCP**: `quoth mcp --db PATH` registers exactly one tool, `evidence_bundle`,
  with bounded, described inputs and an `answer_use` caller contract.
- **Core-memory ledger**: typed claims with origin and authority, quote verification
  against the agent's chat history, write-time reconcile, bitemporal history that never
  deletes, guided review with turn-bound approvals and undo, and a published core block
  (`quoth ledger`).
- **Episode lanes**: searchable records of what the assistant said and what the owner wrote
  (`quoth episodes`).
- **Sidecar and Hermes plugin**: `quoth sidecar` serves core, recall, evidence, review and
  lanes over a Unix socket (`quoth-sidecar/v1`); a stdlib-only Hermes memory provider with
  four tools, a `/quoth` command and two skills (`quoth hermes-plugin install`).
- **Relevance gate and reranker**: optional thresholds on cosine, word overlap or a
  cross-encoder (`bge-reranker-v2-m3`) for what recall injects (`quoth rerank check`).
- **WebUI panel**: read-only memory views for the Hermes WebUI (`python -m quoth.control`).
- **Evals**: eval set v1 with a JSON Schema, a harness that scores the real prefetch path,
  receipts, compare, threshold sweeps and an online prefetch-log readout (`quoth eval`,
  `quoth.evals`), plus a public synthetic fixture.
- **Operations**: `quoth health` for host monitors, deployment templates in `deploy/`, and a
  CI workflow with hash-locked wheel installs, a security job and a gated release guard.
- **Models**: `quoth models fetch` downloads the pinned BGE-M3 and reranker files and keeps
  each only if its SHA-256 matches; nothing downloads at run time. A Docker image
  (`compose.yaml`) runs the real-model tests and evals with models in a volume.

Snapshots and evidence databases built by earlier development versions do not open with this
release (new schema, chunker and embedder input). Rebuild them with `quoth snapshot`; the
steps are in [`docs/FRESHNESS.md`](docs/FRESHNESS.md#rebuilding-across-an-index-change). The
ledger and the episode-lane databases keep their data and need no rebuild.
