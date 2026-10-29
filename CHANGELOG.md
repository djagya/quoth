# Changelog

Notable changes, newest first. Versions follow `version` in `pyproject.toml`; releases are
the `v*` tags.

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

Snapshots and evidence databases built by earlier development versions do not open with this
release (new schema, chunker and embedder input). Rebuild them with `quoth snapshot`; the
steps are in [`docs/FRESHNESS.md`](docs/FRESHNESS.md#rebuilding-across-an-index-change). The
ledger and the episode-lane databases keep their data and need no rebuild.
