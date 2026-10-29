# Evaluation

One standard for measuring recall. `quoth.evals` is the evaluation API and `quoth eval` the
CLI:

| Command | What it does |
|---|---|
| `quoth eval validate SET` | check a set and its manifest against eval set v1 |
| `quoth eval convert --from golden-v1\|golden-v3\|ledger-updates IN OUT` | convert another format to eval set v1 (writes OUT and its manifest) |
| `quoth eval fixture [SRC] --out DIR [--model M]` | build a fixture's stores (snapshot, ledger, `state.db`, both lanes) from text; `SRC` defaults to the shipped `eval/public` |
| `quoth eval run SET …` | score a set against the real prefetch path and write a receipt |
| `quoth eval compare A B [--tolerances FILE]` | gate-style diff of two receipts |
| `quoth eval sweep RECEIPT [--max-loss 0.01]` | replay relevance-gate thresholds on a receipt's candidates |
| `quoth eval online --log DIR [--days N]` | aggregate readout of the sidecar's prefetch log |

```python
from quoth.evals import load_set, validate, run_set, Receipt, InProcessTarget, compare

evalset = load_set("eval/public/set.jsonl")          # validates the set and its manifest
target = InProcessTarget.build(db=..., ledger=..., model="hash-ngram")
receipt, _ = run_set(evalset, target)                 # one dry-run prefetch per row
receipt.write("run.json")                             # ids and metrics only, no text
```

Privacy: public sets and fixtures are synthetic and live in `eval/public/`. Keep private
sets outside the repository and load them by path. Nothing the tools print or write by
default contains question text, note paths or excerpts.

## Eval set v1

A set is a JSONL file plus a sibling manifest (`set.jsonl` → `set.manifest.json`). JSON
Schema: [`eval/schema/evalset-v1.schema.json`](../eval/schema/evalset-v1.schema.json) (the
row, with the manifest under `$defs/manifest`). `eval validate` checks the schema and the
cross-field rules below.

### Row

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | `[A-Za-z0-9._-]{1,128}`, unique in the set |
| `question` | yes | 1–2048 characters, exactly what the user typed |
| `lang` | yes | language code (`en`, `ru`, `pt-BR`) |
| `asked_at` | no | ISO date or datetime the question was asked |
| `context` | no | preceding turns `[{role: user\|assistant, text}]`, oldest first |
| `kind` | yes | `fact`, `preference`, `person`, `project_state`, `decision`, `temporal`, `knowledge_update`, `unanswerable`, `episodic`, `followup` |
| `origin` | yes | `real_user` (the owner typed it), `assistant_query` (the assistant asked memory), `authored` (written for the set), `synthetic` (made up) |
| `expect` | yes | see below |
| `tags` | no | free strings (`scope:work`, `legacy_kind:adversarial`) |
| `meta` | no | source-format fields kept for exact round-trips |

`expect`:

- `abstain` (bool): nothing should be injected. It is true exactly when `sources` is empty;
  `unanswerable` rows always abstain; `followup` rows need `context`.
- `sources`: what counts as the answer, any of which may match: `{store, match}` with
  `store` one of `vault` (snapshot evidence), `claims` (accepted ledger claims), `owner`
  (the owner lane), `assistant` (the assistant lane), and `match` exactly one of
  - `{path}`: a vault-relative note path (vault only);
  - `{locator}`: `path:start-end`, `hermes:<session>:<message>`, or `claim:<id>`;
  - `{claim_text_contains}` or `{subject_key}`: claims only;
  - `{text_contains}`: a case-insensitive substring of the candidate's text.
- `stale` (optional): sources that must not be injected (superseded values of a knowledge
  update).
- `answer_hint` (optional): private sets only; never printed or copied into a receipt.

### Manifest

```json
{"schema": "quoth-evalset/v1", "privacy": "public|private", "sha256": "<of the JSONL bytes>",
 "rows": 29, "counts": {"kind": {}, "origin": {}, "lang": {}, "abstain": {"true": 3, "false": 26}},
 "fixture": "eval/public", "source": {"format": "golden-v1", "sha256": "<of the converted input>"}}
```

`fixture` names the store set the rows target. Converters and `write_set` write the
manifest; a private set and its manifest are written with mode 0600.

### Converters

| `--from` | Input | Mapping |
|---|---|---|
| `golden-v1` | `{id, question, lang, expected_paths, scope, kind}` rows (e.g. `eval/golden.jsonl`) | paths become vault sources; `adversarial` → `unanswerable`, `project` → `project_state`, unknown kinds → `fact`; the original kind and scope are kept as `legacy_kind:` and `scope:` tags; `--origin` (default `authored`) |
| `golden-v3` | rows with `asked_at`, an origin (`real_user`, `assistant_query`, `authored`) and optional `answer_hint` | paths become vault sources; `project-state` → `project_state`; `answer_hint` → `expect.answer_hint`; `domain`, `post_snapshot`, `source_kind`, `temporal_kind`, `expected_event` → `meta`. When a package `manifest.json` sits next to the input, its `dataset.sha256` must match |
| `ledger-updates` | `{id, steps, query, current, stale}` | a `knowledge_update` row with `current` as a claims source and `stale` as stale sources; the steps go to a ledger seed (`OUT.ledger-seed.jsonl` or `--seed-out`) |

Both golden mappings have exact inverses, so converting back gives the original rows. Default
privacy is `private`; pass `--privacy public` only for synthetic input.

## The harness: `eval run`

Each row is one `POST /v1/prefetch` with `dry_run: true` (nothing is marked used) and
`k_candidates` (default 10, max 20). In a dry run the sidecar returns, besides what it
injected, `detail.candidates`: one ranked list per store before injection (claims, vault
evidence, and both lanes searched without their score floor), each item flagged `injected`.
Serving is unchanged: the injected part is exactly the prefix each store serves. Vault and
claim candidates also carry the relevance gate's view: `served` (in the prefix serving would
inject), `lexical` (share of the query's content words in the item; vault hits also
`matched`, the count; claims also `gate_score`), `passed` and `gate` (the reason). With a
reranker loaded, they also carry `rerank` and `rerank_ms`.

Targets:

- **In-process** (default): a sidecar built from `--db --ledger --state-db --episodes
  --owner-episodes --model`, or from `--fixture-dir` (a directory built by `eval fixture`),
  with the sidecar's own flags (`--evidence-budget`, `--episodes-prefetch`,
  `--owner-prefetch`, `--episode-min-score`, `--owner-min-score`, the relevance gate and the
  reranker flags). It embeds all accepted claims first and waits for background searches
  between rows. A missing reranker model stops the run (exit 2).
- **`--socket PATH`**: a live sidecar. Read-only, shares the loaded model, and runs with the
  sidecar's own gate (recorded from `/v1/health`); gate and rerank flags are refused here.
  The evidence budget defaults to 30 seconds.

`--gold-drift` (needs `--db`) resolves expected vault paths against the snapshot with the
nightly gate's rules: a path renamed to exactly one current path is followed, a lost path
makes the row stale and removes it from vault scoring, and past the bound the run ends with
`status: gold_drift_excessive` (exit 3). `context` and `asked_at` are recorded but not used
yet (`config.context: "ignored"`).

### Metrics

All aggregate: overall and by `kind`, `origin` and `lang`, per store (`claims`, `vault`,
`owner`, `assistant`) inside each block.

| Metric | Definition |
|---|---|
| `stores.S.hit@k` (k 1, 3, 5, 10) | rows with an expected source in S where one is in S's top-k candidates |
| `stores.S.recall@k` | mean fraction of a row's expected S sources found in the top-k |
| `stores.S.mrr` | 1/rank of the first expected S source (0 past rank 10) |
| `stores.S.injected_hit_rate`, `injected_precision`, `injected_rate` | per store: expected source injected; share of injected S items that match; rows with any S injection |
| `injected_hit_rate` | answerable rows where an expected source is in the injected block |
| `injected_mrr` | 1/position of the first expected source in the injected block (claims, vault, owner, assistant order) |
| `injected_precision` | share of injected items matching an expected source (answerable rows with injection) |
| `empty_rate` | rows with nothing injected |
| `abstain_nonempty_rate` | abstain rows that still got an injection (lower is better) |
| `stale_leak` / `stale_leak_items` | rows / items where a stale source was injected (must stay 0) |
| `latency_ms.STAGE.{p50,p95,max}` | `wall` (harness), `total`, `claims_lexical`, `vault`, `claims_dense`, `owner`, `assistant`; nearest-rank percentiles |

`stores.vault.hit@5`, `hit@10`, `mrr` and `recall@10` are the nightly gate's metrics: the
gate scores with the same candidates and matching.

### Receipt

`--out FILE` (and stdout unless `--quiet`):

```json
{"schema": "quoth-eval-run/v1", "created_at": "…", "code": {"package": "quoth", "version": "…"},
 "set": {"schema", "sha256", "rows", "privacy", "fixture"},
 "config": {"mode", "model", "evidence_budget", "stores", "prefetch", "min_score", "gate", "k_candidates",
            "gold_drift", "rerank": {"model", "repo", "revision", "budget", "claims", "max_tokens"}},
 "status": "ok|gold_drift_excessive", "gold_drift": {"renamed", "stale", "ids"}, "scored": 29,
 "metrics": {"overall", "by_kind", "by_origin", "by_lang"}, "latency_ms": {}, "statuses": {},
 "items": {"vault_hit@10": {"<id>": true}, "injected_hit": {"<id>": false}},
 "candidates": {"schema": "quoth-eval-candidates/v1", "rows": {"<id>": {
   "abstain": false, "answerable": true, "has_stale": false,
   "stores": {"vault": {"expected": 1, "items": [
     {"score": 0.61, "lexical": 0.5, "matched": 2, "match": [0], "served": true, "injected": true,
      "rerank": 0.93}]}}}}}}
```

Row ids are the per-row key. `candidates` keeps, per row and store, each candidate's score,
lexical coverage, which expected sources it matches (indices; `stale` when it matches a stale
source), and whether serving would inject it: the input of `eval sweep`. The harness refuses
to return a receipt that contains any question, expected path, hint or context text of the
set. `--detail-out FILE` writes per-row candidate ids, locators and scores (no text) with mode
0600; locators include note paths, so keep it private.

### Compare and exit codes

`eval compare A B [--tolerances FILE]` checks B against A. Tolerances are data: a JSON object
`{"require_same_set": true, "max_lost_gold": 0, "metrics": {"<dotted path into the receipt>":
{"max_drop" | "max_rise" | "max_rise_ratio": number}}}` merged over the defaults
(`quoth.evals.DEFAULT_TOLERANCES`: vault hit@5, hit@10 and recall@10 may not drop, vault MRR
−0.02, injected hit and MRR −0.02, injected precision −0.05, abstain_nonempty +0.05,
stale_leak +0, claims hit@5 −0.02, lanes hit@5 −0.05, wall p95 +50 %). Lost gold = ids with
`vault_hit@10` true in A and false in B.

| Exit | `eval run` | `eval compare` | `eval validate` |
|---|---|---|---|
| 0 | scored | pass | valid |
| 1 | – | regression past a tolerance, or lost gold | invalid set |
| 2 | unusable input (set, target) | unreadable receipt or tolerances | – |
| 3 | gold drift excessive | not comparable: different set, or B not ok | – |

## Relevance gate and `eval sweep`

Without a gate, prefetch injects every served item: the top 3 vault hits and up to 6 claims,
whatever their score, so a question with nothing relevant still gets a full block. The
relevance gate (`quoth.relevance`) judges what serving would inject and keeps only what
clears a bar. It never changes retrieval or the candidate lists, so `hit@k` and the nightly
gate are unaffected. Every threshold is off by default.

**Scores.** Vault evidence is dense-only: a hit carries its cosine to the query (normalized
vectors). Cosine is an absolute similarity, so one floor means the same thing for every
query, but its level shifts with query length and language; the gap to the turn's top hit is
the query-relative signal. Meaning-matched claims carry their cosine; word-matched claims have
none.

**Words.** `lexical` is the share of the query's content words found in the item's text,
`matched` how many. Words are casefolded (ё read as е); function words of en, ru, pt and de,
conversational filler and 1–2 letter words are dropped; a word loses one inflection ending and
is cut to 6 letters; numbers stay whole.

**Rules.**

| Flag | Effect | Reason when it decides |
|---|---|---|
| `--vault-min-cosine X` | drop vault hits under X | `below_floor` |
| `--vault-max-gap X` | drop vault hits more than X below the turn's top hit | `gap` |
| `--vault-lexical-rescue X` | a hit with this share of the query's words (at least 2) passes anyway; needs one of the two above | `lexical` |
| `--claims-min-cosine X` | a meaning-matched claim needs this cosine | `cosine` / `below_floor` |
| `--claims-min-lexical X` | a word-matched claim needs this share of the query's words | `lexical` / `weak_words` |

A vault hit that passes neither rule fails; one no rule touches passes `cosine`. An item
without a score passes (`unscored`). A claim's gate score is its cosine in the serving list
only, so a dry run with deeper candidates judges it exactly as a live turn does. A dropped
slot stays empty, and when nothing passes the vault section is left out. `/v1/health` reports
the thresholds and `vault_on`/`claims_on` under `relevance_gate`.

**Sweep.** `eval sweep` replays `relevance.Gate` on a receipt's candidates exactly as the
sidecar would and scores each config with the harness metrics. Ranked metrics count an
expected source only if it passes the gate. Default grids: vault floors in 0.01 steps over the
recorded scores × gaps (off, 0.02–0.2) × rescues (off, 0.5, 0.75, 1.0); claim floors in 0.01
steps × word shares (off, 0.25–1.0); then the 5 best of each combined. `--vault-cosines`,
`--vault-gaps`, `--vault-rescues`, `--claims-cosines`, `--claims-lexicals` override a grid
(comma lists, `none` = off).

A config is eligible when the injected hit rate and the vault's and claims' injected hit
rates each lose at most `--max-loss` (default 0.01) against no gate. The recommendation
maximizes injected precision + (1 − abstain_nonempty_rate), then hits, then keeps the most
items and sets the fewest thresholds. The table shows the vault floor curve, the best
eligible configs and the recommended sidecar flags; `--out` writes every config as JSON and
`--format json` prints it. Output holds configs and aggregates only.

```sh
uv run quoth eval fixture --out /tmp/quoth-fixture
uv run quoth eval run eval/public/set.jsonl --fixture-dir /tmp/quoth-fixture \
    --episodes-prefetch on --gold-drift --out /tmp/run.json --quiet
uv run quoth eval sweep /tmp/run.json --out /tmp/sweep.json
# Check a pick: the same run with the flags, then compare.
uv run quoth eval run eval/public/set.jsonl --fixture-dir /tmp/quoth-fixture \
    --episodes-prefetch on --gold-drift --vault-min-cosine 0.35 --out /tmp/gated.json --quiet
uv run quoth eval compare /tmp/run.json /tmp/gated.json
```

For your own data, run `eval run SET --socket "$QUOTH_HOME/quoth.sock" --db SNAPSHOT
--gold-drift` against the live sidecar, sweep the receipt, and set the chosen flags on
`quoth sidecar`.

## Reranker

Cosine between two separately embedded texts often does not separate relevant from
irrelevant notes well. A cross-encoder reads the query and one passage together and scores
the pair. `quoth.rerank` runs `BAAI/bge-reranker-v2-m3` (multilingual) through ONNX Runtime
on CPU. It is not part of the index contract: no score is stored in a snapshot.

| `--rerank-model` | Files | Download |
|---|---|---|
| `bge-reranker-v2-m3-int8` (recommended) | `onnx/model_int8.onnx` (int8 dynamic quantization) + tokenizer | about 0.59 GB |
| `bge-reranker-v2-m3` | `onnx/model.onnx` + `onnx/model.onnx_data` (fp32) + tokenizer | about 2.29 GB |
| `hash-rerank` | none: hashed character 3-grams, deterministic and non-semantic (tests only) | – |

The files come from the `onnx-community/bge-reranker-v2-m3-ONNX` export, pinned at revision
`6f5ff65298512715a1e669753bc754d2bc8f367b`, and live under
`$QUOTH_MODEL_CACHE/onnx-community/bge-reranker-v2-m3-ONNX/`. Every file's sha256 is pinned in
`quoth.rerank`; a download and every load verify them, and a missing or changed file refuses
to load (`reranker.status: unavailable`). Nothing downloads unless asked:

```sh
quoth models fetch --no-embedder --reranker bge-reranker-v2-m3-int8   # fetch once, verified
quoth rerank check                      # verify, load, score 3 public pairs, print timings
```

It prints the load time and the latency of 3 pairs, short (`ms_3_pairs`) and filled to the
token limit (`ms_3_pairs_full`). Each pair is the query and the hit's text truncated together
to `--rerank-max-tokens` (default 192); the score is `sigmoid(logit)`, 0..1. Threads follow
the CPU quota like BGE-M3 (`QUOTH_EMBED_THREADS` overrides). Loaded, either variant adds up to
about 1.5 GB of peak memory to the sidecar.

**On a turn.** The sidecar loads the reranker in the background after BGE-M3's warm-up
(`reranker.status`: `loading`, `ready`, `unavailable`). It scores on its own worker thread, so
the next turn's evidence search never waits behind it: first the hits the turn reranks, then
(with `--rerank-claims`) the claims it serves, then a dry run's deeper candidates. The turn
waits at most `--rerank-budget` seconds (default 0.8), never past the evidence budget, then
cancels the job. `status.rerank`: `ok`; `partial` (vault scores arrived, claims or deeper hits
did not); `slow`; `busy` (the previous turn's job is still finishing); `loading`,
`unavailable`, `error`; `skipped` (nothing to score); `off` (a score-only live turn). In every
case but `ok` and `partial` the turn keeps its vault hits under the cosine rules: reranking can
cost a turn its rerank decision, never its evidence. Without a rerank threshold the reranker
is score-only: dry runs score, live turns do not.

**Rules.**

| Flag | Effect | Reason |
|---|---|---|
| `--vault-min-rerank X` | drop vault hits under this score | `rerank_floor` |
| `--vault-max-rerank-gap X` | drop hits more than X below the turn's best score | `rerank_gap` |
| `--vault-rerank-widen N` | rerank the top N hits by cosine and serve the best 3 by score | – |
| `--claims-min-rerank X` | a scored claim needs this score | `rerank` / `rerank_floor` |

When every vault hit the turn reranks has a score, these decide instead of the cosine rules
(no lexical rescue); a passing hit gets `rerank`. An unscored claim keeps the claim rules.
Every rerank flag needs `--rerank-model`; the sidecar and `eval run` refuse them alone.

**Sweep.** On a receipt with rerank scores, `eval sweep` adds vault rerank configs (floors
0.001–0.9 × gaps off/0.2–0.95 × widens off/5/10, a widen only up to the run's
`--k-candidates`) and claim rerank floors, with the same constraints and objective. When the
pick is a rerank config it also names the cosine fallback for turns without scores.
`--vault-reranks`, `--vault-rerank-gaps`, `--vault-rerank-widens`, `--claims-reranks` override
the grids. Each scored candidate records its scoring time (`rerank_ms`); the sweep replays the
live `--rerank-budget` and `--evidence-budget` on those times, and a config whose p95 scoring
time exceeds the budget is not eligible (`over_budget`). Score on the machine that serves, so
the times reflect its CPU.

To choose rerank thresholds: run the sidecar score-only (`--rerank-model
bge-reranker-v2-m3-int8 --rerank-claims`), wait for `reranker.status: ready`, run `eval run
--socket` at a quiet hour (dry runs hold the single rerank worker), check that
`statuses.rerank` is `ok` for every row, sweep, and set the pick plus its cosine fallback.

## Public fixture: `eval/public/`

Synthetic text only:

- `vault/`: ten notes (English plus one Russian), indexed by the normal ingest and embed path;
- `ledger-seed.jsonl`: accepted, pending and rejected claims plus knowledge-update chains;
- `telegram.jsonl`: Hermes messages; `eval fixture` writes them into a Hermes-shaped
  `state.db` and ingests both lanes (a scheduled-run row and a too-short reply are dropped);
- `set.jsonl` and its manifest: 29 rows covering every kind, including follow-ups with
  `context`, knowledge updates with `stale`, unanswerables and lane rows.

```sh
uv run quoth eval fixture --out /tmp/quoth-fixture      # hash-ngram, no model files
uv run quoth eval run eval/public/set.jsonl --fixture-dir /tmp/quoth-fixture \
    --episodes-prefetch on --gold-drift --out /tmp/run.json --quiet
uv run quoth eval compare /tmp/run.json /tmp/run.json
```

`hash-ngram` is not a semantic model, so fixture numbers check plumbing, not quality. CI runs
the fixture in `tests/test_evals.py` and against the installed wheel in `scripts/ci_smoke.py`.

## Online measurement: the prefetch log

`quoth sidecar … --prefetch-log DIR [--prefetch-log-days 30]` (off when omitted) appends one
JSON line per real (non-dry-run) prefetch to `DIR/prefetch-YYYY-MM-DD.jsonl` (UTC, mode 0600,
directory 0700):

```json
{"type": "prefetch", "ts": "…", "v": "0.15.0", "session": "<sha256(session_id)[:16]>",
 "query_sha256": "…", "query_len": 42,
 "stores": {"claims": [{"id": 12, "score": 0.71, "via": "dense", "injected": true}],
            "vault": [{"locator": "PROJECTS/x.md:3-9", "score": 0.58, "served": true, "lexical": 0.5,
                       "matched": 2, "passed": true, "gate": "cosine", "injected": true}],
            "owner": [{"locator": "hermes:S:M", "score": 0.66, "injected": true}]},
 "status": {"vault": "ok", "owner": "ok", "assistant": "off"},
 "ms": {"claims_lexical": 0.4, "vault": 41.0, "claims_dense": 0.2, "owner": 12.1, "total": 55.3}}
```

It also writes one line per `evidence_bundle` and `episode_search` tool call (`{"type": "tool",
"tool", "session", "k", "who", "results": [locators]}`) and one per skipped injected turn
(`{"type": "skip", "reason": "not_authored"}`). No query or excerpt text is logged; the query
hash is unsalted and locators carry note paths, so treat the log as private. Writes go through
a bounded queue to one thread and are never fsynced; a full queue or an I/O error drops the
record (warned once), so a turn is never slowed. Files older than `--prefetch-log-days` are
pruned when a new day's file opens.

`quoth eval online --log DIR [--days N]` reads it: turns, sessions, injected rate per store,
empty-recall rate, probable-miss rate (a search tool call in the same session after a turn and
before the next one), score distributions, statuses, query length and latency p50/p95 per
stage. Aggregates only.

## The nightly gate and daily quality

`quoth refresh-nightly` scores the serving snapshot and the successor, and once a day the
served snapshot, with `quoth.evals.gate`:

- **Sets.** `--gate-set PATH` (required) is the gate's `synthetic` dataset. The
  `--private-runner PATH` module and the `real-questions-v2.jsonl` file next to it form the
  `private` dataset (see [FRESHNESS.md](FRESHNESS.md#the-private-gate-runner)).
  `--quality-set PATH` (optional) fills the daily quality's `golden_v3` block.
- **Retrieval.** One dry-run prefetch per row on an in-process sidecar over each snapshot (an
  empty in-memory ledger, one shared BGE-M3, 2 threads, no download). A row whose evidence
  call does not finish is an error, never a miss.
- **Scores.** Per row, hit@5, hit@10, MRR@10 and recall@10 of the expected vault paths among
  the top 10 candidates. Rows without vault gold are not gated; a golden-v1 row that should
  have gold and has none fails the run (`synthetic_gold_missing`, `quality_gold_missing`).
- **Gold drift** follows renamed notes and drops rows whose gold is gone, on both sides;
  more than 10 % stale rows in a dataset, or fewer than 30 rows left after drops, fails
  the run (`gold_drift_excessive`).

Receipts: `quoth-nightly/v1` (switch decision), `quoth-serving-quality/v1` (daily quality),
`quoth-heartbeat/v1`. They hold aggregates and item ids only. The gate rules are in
[FRESHNESS.md](FRESHNESS.md#nightly-refresh).

## Control lab

`control/lab.py` scores retrieval pipeline specs (`quoth.pipeline`) for the WebUI's retrieval
lab, not the served prefetch path, so it does not run on `eval run`. Its ordinary-suite path
metrics come from the harness (`evals.gate.path_scores`); the temporal/abstention and
protected-facet scorers it also needs live in `control/lab_suites.py`. Its suites keep their
own formats (`eval/golden.jsonl`, `eval/q2-controls.json`, `eval/e1-temporal.json`) because
they express chronological answers and snapshot-bound excerpt facets that eval set v1 does
not. The two JSON files are synthetic placeholders (the facet suite is bound to an all-zero
snapshot digest), so a lab evaluation cannot pass on a real snapshot until they are
regenerated for it.
