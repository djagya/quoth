# Changelog

Notable changes, newest first. Versions follow `version` in `pyproject.toml`; releases are
the `v*` tags.

## 0.19.0 — "what was it before", the short rule under rerank, judged sweep precision

No index-contract file and no ledger migration changed since 0.18.2: 0.17 snapshots keep serving.
The plugin changed (`memory-caller` skill 2.1.0): reinstall it and reload the gateway.

### Recall

- **The short-message rule also holds when a rerank rule decides.** `Gate.vault`'s rerank
  branch returned before the short-query check, so with any `--vault-min-rerank` /
  `--vault-max-rerank-gap` a one-word message ("thanks") injected whatever the reranker
  passed. The rule (at most `vault_short_terms` content words: a shared word or
  `vault_short_min_cosine` needed) now applies on both paths; a message that names the note
  still passes. Serving without rerank rules is unchanged.
- **"What was it before": past values in recall.** When a turn asks about the past, prefetch
  now also serves the values its claims replaced, each under its current claim, newest first,
  as an indented `previously (from … until …; replaced by #N, not current)` line. Only an
  accepted value that a newer fact superseded counts: an owner's edit is a rewording of the
  same value, and forgotten, rejected, expired and pending claims never come back. Past values
  share their claim's gate verdict and the claims cap (6, at most 3 per claim). Past intent
  is a small lexical detector in English, Russian and German ("before", "used to",
  "previously", "earlier", "back then", "what was", "раньше", "до этого", "früher",
  "damals", a past year or date; weak words need a past-tense verb, so "Before I go, …" is
  not one). The past path is one recursive SQL query; other turns run nothing extra.
- `/v1/prefetch` takes `include_history` (true forces the past path, false turns it off) and
  reports `history: {asked, items}`; `evidence_bundle` takes `include_history` and then adds
  `memory_history` (matching memories with the values they replaced), so the assistant can ask
  for history when recall did not show it. Without the flag the tool's reply is unchanged.
- The plugin guide, the `memory-caller` skill (2.1.0), `/quoth recall` and the WebUI recall
  view show past values as past, never as current facts.

### Evals

- **The sweep no longer rewards injecting no claims on sets without claims gold.** Block
  `injected_precision` counts every injected claim or episode that is not gold as wrong, so on
  a set with no claims gold a claims gate that drops all claims looked like a precision gain.
  New `judged_precision` counts only items whose rightness is known (vault items, claims on
  `claims_judged` rows, any matched item); the sweep's objective uses it, so such a gate ties
  and the gentler config wins. `injected_precision` is unchanged in receipts.
- **`follow` on claim anchors.** `current` (default, as before), `edits` (rewordings only:
  the value as it was; a superseded value resolves as `historical`, reported in
  `claims_gold.historical`) or `none` (that exact claim; an old wording is `edited`). A past
  value injected under its claim matches such gold like any claim. `stores.claims.history_rate`
  counts rows where a past value was injected; `eval sweep` replays past values with their claim.
- `eval label-claims` also offers past values (marked, `follow: edits` once picked) to rows
  whose question asks about the past.
- The public claims set gains three "what was it before" rows and one past-intent false
  positive (10 rows).

## 0.18.2 — clear dropped copies, a stale-core health check, stricter presort, claims gold

No index-contract file and no ledger migration changed since 0.18.1: 0.17 snapshots keep serving.

### Evals

- **Claims gold that survives a changing ledger.** A row can name an expected claim by an
  anchor, `{"store": "claims", "match": {"claim": {"text", "id"?, "subject_key"?}}}`. `eval
  run` resolves it against the target ledger at the start of a run: by id when its text still
  matches, else by text, else by `subject_key`, then forward through edits and supersedes to
  the claim that is accepted and current now. Unresolved gold (`not_found`, `ambiguous`,
  `pending`, `rejected`, `expired`, `forgotten`, `no_ledger`) is reported in the receipt's
  new `claims_gold` section and taken out of the row, never counted as a miss: a row left
  with nothing to score is skipped. A `--socket` run resolves against `--ledger`, read-only.
- **Judged claims precision.** `expect.claims_judged: true` says a row's claims sources are
  every claim that answers it (possibly none). `stores.claims.judged_n`, `judged_precision`
  and `judged_items_mean` then say how many injected claims were right; `eval sweep` reports
  `claims_judged_precision` and suite headlines carry the claims numbers when a set has them.
- **`quoth eval label-claims SET --ledger DB --out NEWSET`** proposes claims gold for a set's
  answerable rows from the ledger's accepted claims (dense with a local model, else word
  overlap) as `review: pending` rows, which are not scored. **`quoth eval review-claims
  NEWSET`** lets the owner pick the right claims, say none is right, or dismiss, row by row.
  Nothing is confirmed without the owner; NEWSET is private and the commands print counts only.
- `provenance.review` gains `pending`; the v2 JSON schema has the anchor and `claims_judged`.
- The public fixture gains `eval/public/claims.jsonl` (6 synthetic claims-gold rows, run by
  the `public` suite as `claims`), so CI covers resolution and judged precision.

### Diagnostics

- **Proposal audit.** `deploy/diagnostics/quoth-proposal-audit.py` (stdlib, read-only,
  counts only) reports where rejected proposals come from: outcomes by origin, proposer, kind,
  presort suggestion, mirror action, text length and shape, plus presort precision, time to
  decision, re-proposals after a rejection and repeated rejected texts. Not in the wheel.

### Review

- **Stricter presort (memory-review skill 3.7.0).** A ledger audit showed the owner kept almost
  none of what presort marked `accept` without rewording it, and none of the setup/environment
  or project-status notes or the `keep` items. Presort now suggests `accept` only for durable
  facts about the owner (identity, people, preferences, consent, corrections), always with the
  final wording in `text`; setup notes, project status and the assistant's own notes go to
  `covered`, `stale` or `reject`; `keep` is retired (unsure → `reject` with a note). An
  operator-requested re-presort re-suggests every pending claim.
- **Copies of memories you already dropped clear in one step.** Releases before 0.18.1
  restored an original's confirmed copies to pending when the owner dropped the original.
  Those copies now form a junk bucket, `dropped_copies`: pending, latest decision `restored`
  with `target_dropped` = X, X's latest decision the owner's `rejected`, nothing decided
  since. Copies restored by expiry by age or latest-wins are not in it. Applying the bucket
  (`apply bucket=dropped_copies bucket_stamp=…`, or a page by `ids`) rejects each as a
  duplicate of its original under the approval's batch, so `undo` returns them. No presort
  suggestion is needed; the other junk buckets leave these copies out. The digest and ledger
  health show the count (`dropped_copies`).
- The memory-review skill (3.6.0) offers them first in the bulk clean-up: "N copies of
  memories you already dropped. Clear these? (ok / skip / show)".

### Health

- **The sidecar keeps the published core current outside reviews too.** At startup it publishes
  the core when the published one is missing or stale (`core_freshness`), and an auto-accepted
  proposal republishes it like a review does (`core` in the propose reply). `/v1/core` serves
  the published file once one exists, so a core left behind by an operator's CLI accept or an
  older release is no longer served until the next review.
- **A stale core turns health red.** With a core directory (`/v1/health` with `--core-dir`,
  `quoth ledger health --core-dir DIR`), health compiles the core in memory and compares its
  claim ids with the published `core.json`: `core: {status, published_sha, missing, extra,
  compiled_at}`, counts only. Red with `core_stale` when they differ, `core_unpublished` when
  nothing is published while accepted core claims exist, `core_unreadable` for a broken
  manifest. A reordering by use counts is not staleness.

## 0.18.1 — a dropped memory takes its copies; the sidecar republishes the core

No index-contract file changed since 0.18.0: 0.17 snapshots keep serving.

### Review

- **Dropping a memory drops its confirmed duplicates.** An owner's reject (`reject`, or
  `apply` of a junk suggestion) no longer brings back the copies the owner had earlier
  confirmed as its duplicates. The whole family goes in the same transaction and under the
  same approval: copies already rejected stay rejected, and copies that are pending only
  because the system restored them are rejected again (`family_of`), so `undo` returns them
  with the original. The family is transitive (copies of copies). Pending `duplicate`
  suggestions, copies the owner decided again, and accepted claims are never touched. Reject
  results carry `family: {root, ids, rejected_now, already_rejected}` in place of
  `restored_duplicates`, and the re-rejects count toward the 200-decision approval cap.
  Expiry by age and latest-wins still restore copies: the owner never decided those.
- The memory-review skill (3.4.0) reports a drop as "Dropped #X and its N confirmed copies."
- **The sidecar republishes the core after a review.** The assistant's gateway has no `quoth`
  command, so the core it was told to publish could go stale after accepts. With `--core-dir`,
  each review decision that changes accepted memory (accept, edit, forget, undo_auto, an
  apply that accepts, an undo that reverts accepts) now publishes the core under the
  sidecar's lock at the budget it serves, and the reply carries `core: {published, claims}`.
  A failed publish is reported as `core: {error, reason}` and never fails the decision.
  Health shows the last attempt (`core_publish`). The memory-review skill (3.5.0) tells the
  owner the core was updated and runs `compile-core --publish` only on a core error.

### Evals

- `eval sweep` judges configs against the live gate, not against no gate. The reference is
  the receipt's recorded gate (`config.gate`), else the serving default. A config is
  eligible only when it loses no answerable row the reference injects (`--max-lost-hits`,
  default 0), keeps the injected hit rates within `--max-loss`, and does not raise
  abstain_nonempty_rate by more than `--max-loss`. It is recommended only when it beats the
  reference on the objective; otherwise the sweep says to keep the live gate. A sweep
  against no gate could recommend a config worse than the gate already running.
  `--reference none` keeps the old comparison.
- `eval sweep R1 R2 …` (or `NAME=PATH`) sweeps several receipts at once: a config must be
  eligible on each, configs rank on the pooled rows, and the output gives each receipt's
  reference and recommended metrics. A pick tuned on one golden set can lose answers on
  another or raise false injection on the no-memory set.
- Rerank and claims configs keep the reference's short-query rule, and claims configs its
  vault floor and gap. Before, rerank configs dropped the short-query rule and a claims
  pick turned the vault gate off.
- The sweep's JSON is now `quoth-eval-sweep/v2`. `lost_hits` and `loss` are measured
  against the reference. New fields: `reference`, `reference_gate`, `max_lost_hits`,
  `rows`, `gained_hits`, `abstain_rise`, and `sets` (per receipt; with several receipts
  each config also carries its per-receipt verdict). `baseline` is still the no-gate row.
  `receipt` is only present for a single receipt.

## 0.18.0 — eval set v2 and suites, a public benchmark, quieter short messages

No index change: 0.17 snapshots keep serving, no rebuild. Install the release and restart
the sidecar. The short-message gate rule is on by default; miss capture and the suite gate
are opt-in (`--capture-misses`, `refresh-nightly --suite`), and the nightly's old gate flags
keep working.

### Recall

- **Short messages need a shared word or a close match.** A query of at most 1 content word
  ("thanks", "good night", "ты бот?") injects a vault hit only when the hit holds that word or
  reaches cosine 0.60 (`--vault-short-terms`, `--vault-short-min-cosine`; `off` restores the
  old gate). Short queries embed close to many notes. On a full real-vault snapshot, the
  no-memory set's false-injection rate falls from 41% to 31% (12 rows fixed, none broken, exact
  p 0.0005), and no golden-v1 or golden-v3 row loses its injected answer. On the public
  benchmark the injected hit rate is unchanged (0.695). A first draft (2 words, cosine 0.65)
  cut false injection to 26% but cost the benchmark 14 injected hits, mostly short
  cross-language questions, which share no word with their note; the defaults were tuned on
  the benchmark's dev split. Receipts record each row's word count, so `eval sweep` replays
  the rule.

- **The reranker reads the note title, heading path and chunk text** (up to 1,200
  characters), not only the 360-character excerpt the block injects; `--rerank-max-tokens`
  defaults to 512 (was 192). On a full real-vault snapshot, reranking the bare excerpt ranked
  the gold note into the top 3 less often than cosine order (golden-v1 0.714 vs 0.755,
  golden-v3 0.648 vs 0.833); with the context, more often (reranking the top 5: 0.837 and
  0.870). Each set gains 3 rows, not yet significant (exact McNemar p 0.51 and 0.38), so rerank
  rules stay opt-in. Score-only serving (a reranker without rules) is unaffected. Cost: about
  0.75 s for 5 pairs on 4 cores, so a rule needs a sweep on the serving machine first.

### Evals

- **Mining real questions (opt-in).** `quoth sidecar --capture-misses DIR` keeps the owner's
  question text, on the host only (0600 file in a 0700 directory), for likely misses: the
  assistant searched memory within 180 s after a turn that injected no vault evidence
  (`searched_after_empty`), whose pool all failed the gate (`gate_rejected_pool`), or that
  injected evidence anyway (`searched_despite_injection`). Candidates carry the injected and
  search-returned paths as candidate gold, merge identical questions, and are capped at 500
  and 30 days. `quoth eval review DIR --out SET` walks them in a terminal: pick the gold,
  abstain with a reason, edit, reject (the text is deleted) or skip; accepted rows go to a
  private v2 set as `real_user` rows with owner provenance. `--stats` prints counts only. The
  prefetch log stays text-free. Off by default and not in the s6 template.
- **Eval set v2.** Optional row fields, so every v1 row stays valid:
  - all-of evidence `groups` for questions that need several notes;
  - judged look-alike `negatives`;
  - `split` (dev/test) and `cluster`;
  - `hops`;
  - `abstain_reason` (`no_memory_needed`, `not_in_vault`, `false_premise`, `near_miss`);
  - label `provenance` with a review state (drafts and rejected rows are not scored);
  - `paraphrase_of`.

  Receipts add `groups.all@k`, `group_recall@10`, `injected_all_rate`, `negative_leak_rate`,
  breakdowns by split, abstain reason and hops, and 95% Wilson intervals of the headline
  rates (`metrics.ci95`). `eval run --split` scores one split.
- **Paired significance tests in `eval compare`** (`paired`): exact McNemar on per-row
  hits and a sign-flip permutation test on reciprocal ranks, over the rows both receipts
  scored. Reported next to the tolerance checks; they never change the exit code.
- **Suites** (`quoth eval suite validate|run|compare`): one file lists sets with a role
  (`smoke`, `regression`, `quality`, `tune`), so public and personal sets run the same way on
  one target. A quality set fails a comparison only when a paired test is significantly
  worse. `eval compare`'s McNemar tests report `mde`, the smallest change the set can detect.
- **A portable no-memory set** (`eval/sets/no-memory`, 120 rows, English and Russian):
  messages that need no recall, for the false-injection rate on any vault. On a full
  real-vault snapshot, prefetch injected vault excerpts into 41% of them (95% interval 32–50%),
  mostly short conversational turns: 11 of 12 thanks, 13 of 20 greetings, 8 of 10 questions
  about the assistant, but 1 of 24 general-knowledge questions.
- **`eval convert --from private-v2`** turns the private gate runner's
  `real-questions-v2.jsonl` into an eval set v2 (origin `real_user` unless a row records
  one, provenance author `owner`, fixture `private`). Rows the runner never scored
  (no expected paths, adversarial or unanswerable kinds) become abstain rows.
- **Personal suite hook**: `tests/test_personal_suite.py` (marker `personal`) scores the
  suite in `QUOTH_PERSONAL_SUITE` on the snapshot in `QUOTH_PERSONAL_DB` the way the nightly
  gate does, and with `QUOTH_PERSONAL_BASELINE` requires `suite compare` exit 0 against an
  earlier run; skipped when the variables are unset. `eval/suites/personal.example.json`
  shows a personal suite. `eval compare` tolerances accept `excused_lost_gold` (ids whose
  lost gold does not count).
- **A public retrieval benchmark** (`eval/bench`, `eval/sets/bench`, `eval/suites/bench.json`):
  a fictional persona vault of 72 English and Russian notes with look-alike pairs, and 539 v2
  rows. 416 are answerable (47 need 2–3 notes, 78 carry judged negatives); 123 abstain
  (`not_in_vault`, `false_premise`, `near_miss`). Split dev/test by topic cluster. A blind
  check (the same model in a fresh context, without the labels) agreed with 538 of them.
  With BGE-M3 on the test split: vault hit@10 0.99, MRR 0.95, injected hit rate 0.70 (95%
  interval 0.63–0.75), false injection on 42% of abstain rows. A paired test detects about 6
  points there, against about 12 on a 50-row personal set. `eval fixture` now builds a
  fixture without `ledger-seed.jsonl` or
  `telegram.jsonl` (empty ledger and lanes).

### Nightly

- **`refresh-nightly --suite SUITE`** gates the successor on an eval suite instead of a
  gate set plus a private runner: both snapshots are scored on the suite's sets and compared
  set by set. A failing `smoke` or `regression` set holds the switch (`suite_<id>_regressed`),
  a `quality` set only when a paired test is significantly worse (`suite_<id>_quality_worse`);
  `tune` sets are not scored, latency is not gated. Content-only refreshes keep the legacy
  gate's allowances (drops up to 0.03, lost gold excused when its note changed). The suite's
  fixture must be `private` (or `--suite-fixture NAME`). The receipt's `suite` block holds
  headlines with 95% intervals and the paired tests per set; the per-set run receipts stay in
  `<receipts>/suite-runs/<receipt id>/` (newest 14 kept). No Python file is executed.
- `--gate-set`, `--private-runner` and `--quality-set` work as before but cannot be combined
  with `--suite`. `--private-runner` is deprecated: receipts record `deprecated:
  ["private_runner"]`, and a run at a terminal prints a note (a scheduled run stays silent).

## 0.17.1 — one hit per note in the prefetch pool, nightly self-check fix

No index change: 0.17.0 snapshots keep serving, no rebuild. Install the release and restart
the sidecar.

### Recall

- **The prefetch pool holds at most 1 hit per note before any repeat** (`MAX_HITS_PER_NOTE`),
  as the served slots already did. Before, a long note whose chunks all match a query filled
  the 10 candidates a turn judges and the nightly gate scores. On a full real-vault snapshot,
  dense retrieval: golden-v1 hit@10 0.898 → 0.939 (the 0.14 level), hit@5 0.816 → 0.857, MRR
  0.606 → 0.620; golden-v3 hit@10 0.926 → 0.944; injected hit rate unchanged. Same-note
  repeats among injected hits fall from 28 to 4 over both sets, which lowers the
  injected-precision metric (it counts every chunk of a gold note).
- Expected change in gate scores: the candidate list is no longer the plain chunk ranking, so
  a nightly receipt scored before this change and one scored after are not comparable item
  by item. The first refresh after upgrading compares like with like (both sides are scored
  by the running code).

### Fixed

- **The nightly self-check failed on hosts with a model cache outside `$HOME`.** Its
  `quoth mcp` child got only the MCP SDK's default environment, so `QUOTH_MODEL_CACHE` and
  `QUOTH_HOME` were dropped, the tool call failed with `EmbedderUnavailable`, and every switch
  rolled back (`self_check_failed`). The child now gets every `QUOTH_*` variable and
  `HERMES_HOME`. A failed probe call is recorded as `self_check_error` (code and exception
  class, no message text) instead of failing on a JSON decode.

## 0.17.0 — vault exclusion, freshness per hit, opt-in hybrid retrieval

### Index format change: rebuild every snapshot

**The evidence schema and the index-contract files changed. Existing snapshots are
incompatible and cannot be refreshed: build a fresh one with `quoth snapshot`** and switch
the serving pointer to it ([FRESHNESS.md](docs/FRESHNESS.md#rebuilding-across-an-index-change)).
`0001_evidence.sql` was edited in place, so a fresh snapshot has one clean schema.

### Index

- **Exclusion, one path.** `.quothignore` at the vault root (gitignore syntax: comments,
  `!` negation, folder-only `/`, `*`, `?`, `[...]`, `**`, root anchoring) and frontmatter
  `quoth: exclude` keep notes out of the index; `exclude_dirs` in `scopes.toml` and hidden
  names are the built-in defaults that `.quothignore` adds to and cannot override. Excluded
  notes are never read into the index (`vault.py`; ingest and the freshness inventory share
  it).
- **Excluded means purged.** A note that becomes excluded loses every row in the next
  snapshot, current and historical: source, versions, chunks, vectors, entities, lexical and
  link rows (`ingest.purged`). This includes rows earlier releases kept as retired history
  under `ASSISTANT/`. A snapshot holding any row under a built-in exclusion is `invalid`.
- **Rollback re-exposes excluded material.** Older snapshots still hold notes you have since
  excluded; pointing serving back at one serves them again. Delete such snapshots (and
  bundles made from them) once the rebuilt one is accepted.
- **Rules count as vault content.** The manifest records `ignore_rules_sha256`; changing,
  adding or removing `.quothignore` makes a snapshot `stale` (`ignore_rules_changed`).
- **Links table.** Wikilinks, aliases, heading links, embeds and relative Markdown links are
  stored per current chunk (`links`: `target`, `heading`, `line`, `kind`, `status`,
  `target_path`), resolved Obsidian-style on every build: `resolved`, `missing`,
  `ambiguous`, or `excluded` (the excluded note's path is never stored).
- **File facts for staleness.** `source_versions` gains `mtime_ns` and `size_bytes` beside
  `content_hash`, so serving can tell that a note changed since indexing.
- **Recall tiers.** Scopes are declared in `scopes.toml` with `recall = "auto" | "explicit"`
  (`quoth.scopes.recall_tier`, `quoth.scopes.explicit_scopes`); a new `private` scope is
  `explicit`.
- **Manifest `built_at`.** Every snapshot manifest records when it was built (UTC ISO-8601).
- **`quoth snapshot --resume`** continues a killed first build from its checkpoint (same
  target and vault, same code and model digests) instead of failing on the leftover target.
- Bundles carry the `links` table; in a scoped bundle, links to notes outside the scope
  become `excluded`.

### Serving

- **Hybrid retrieval, opt-in** (`--retrieval hybrid`; the default stays `dense`, cosine
  only) for recall and `evidence_bundle` alike (`quoth.retrieval`): BGE-M3 cosine fused by reciprocal rank with the snapshot's BM25 and trigram lists and the notes the
  query names (title, alias or path), so exact names and paths are found. The gate still
  judges each hit's real cosine; a lexical-only hit below the floor passes only when the query
  names its note (gate reason `name`). It is not the default: on the golden sets the
  equal-weight fusion lets the three keyword lists outvote the dense one (golden-v1 hit@10
  0.47 hybrid vs 0.90 dense, golden-v3 0.76 vs 0.93), worst on Russian questions. It stays
  opt-in until the fusion is reweighted and measured.
- **Per-note diversity**: recall serves at most 1 hit per note before any repeat, an explicit
  bundle at most 2; an excerpt repeated at another path is returned once.
- **`evidence_bundle` gains `path_prefix`** (a folder pre-filter, for names shared across
  projects), **`path`** (read one note's chunks in order, optionally `#Heading`: follow a link),
  and **`include_explicit`**. Every hit carries `project` (its top-level folder) and `links`
  (resolved targets as fetchable paths; excluded targets as text only, never a path). A `path`
  that names no served note answers `not_found`, the same for every reason.
- **Freshness per hit**: with `--vault DIR` (sidecar and `quoth mcp`) each hit says `live`:
  `same`, `changed` or `missing`, checked by stat and, only when needed, a hash (at most 10
  stats and 3 hashes per call). Bundle provenance carries `indexed_at` (the manifest's
  `built_at`). The recall block opens its vault section with `Vault indexed as of …`, marks a
  changed note `[changed since indexing: read the live note]` and names a hit's links.
- **Recall tiers**: recall never injects notes of the explicit tier
  (`quoth.scopes.explicit_scopes()`); `evidence_bundle` includes them only with
  `include_explicit`. Both filter before ranking.

### Measurement

- Prefetch-log records carry `snapshot_id`, `retrieval` and the gate settings.
- **Record-only late-tail measurement** (prefetch log schema 2). Each authored turn gets a
  request id before its search is submitted and writes `start` (code digest, config and its
  digest, budgets, gate, k), `submitted` or `busy` (a busy turn starts no job and names the
  running one), `wait` (the effective deadline) and its `prefetch` return line; its evidence
  job writes exactly one `done` line, joined by id in any order: `normal` (the snapshot the
  job searched, raw count, candidate locators and the post-gate would-inject locators with
  the turn's own gate, or `unknown` when the decision needs a rerank that was not computed),
  `exception` (type name only) or `censored` at shutdown. Every line carries the process
  instance; `instance` and `counters` lines (every 300 s and at close: dropped and failed
  lines, jobs in flight, unjoined completions) keep lost lines visible. No query, excerpt
  or claim text, no extra search or rerank, and no change to answers, gates or budgets. The
  0.17 development `late` record is replaced by `done`.
- `eval online` joins each explicit search to its session's last prefetch (`explicit_join`:
  injected, pooled but not injected, never pooled) and reports the late tail of a bounded
  window (`late_tail`; `--since`, `--until`, `--max-turns`): strata by version, code, config
  and snapshot, slow and busy apart, completion against the effective deadline, what 1.5,
  2.0, 2.5 and 2.8 s budgets would add (admissible nonempty vs empty or unknown), request and
  job durations with headroom inside the 3 s client deadline, exact denominators with missing,
  dropped and unjoined lines, and a descriptive note overlap with the next explicit search.
- `eval run` scores the explicit bundle beside the prefetch (`explicit` in the receipt),
  records `config.retrieval`, takes `--retrieval` and `--vault`, and `--trace-out FILE` writes
  a private per-row trace (query, exact recall block, explicit hits) for caller tests;
  `--oracle FILE` joins a caller's oracle source text to it by row id, into a separate
  private file (`--oracle-out`).

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
