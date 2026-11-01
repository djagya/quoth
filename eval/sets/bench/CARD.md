# bench: the public retrieval benchmark

**What it measures.** Whether prefetch finds and injects the right note of a personal vault,
and stays quiet when it should: vault retrieval (hit@k, MRR), what reaches the block
(`injected_hit_rate`, `injected_precision`), multi-note questions (`groups.all@k`,
`injected_all_rate`), look-alike notes that must not be injected (`negative_leak_rate`), and
false injection on questions the vault cannot answer (`abstain_nonempty_rate` by
`abstain_reason`). It is the large public set behind claims of a few points: a paired test on
its test split detects changes of about 5–6 points where a 50-row personal set needs 12.

**Fixture.** `eval/bench/vault/`: 72 Markdown notes of one fictional person (English and
Russian), with no ledger and no chat lanes, so only the vault holds evidence. Projects, trips,
health, recipes, family and friends, books, work logs, a cat, a garden plot, dated journal
entries. Every person, company, address and number is invented; real places and published
books appear only as public facts. Notes come in clusters with deliberate look-alikes: two
Portugal trips, two Caucasus trips, two hut treks, three breads, three soups, two curries,
two balcony tomato seasons, an old and a new running plan, budget and Tidepool plan, two
incidents, two transport decisions. Every note is the expected answer of at least two rows
(hole audit, checked by the tests).

With the real model (the dense extra and BGE-M3 files, e.g. in the quoth image):

```sh
quoth eval fixture eval/bench --out /tmp/bench --model bge-m3
quoth eval run eval/sets/bench/set.jsonl --fixture-dir /tmp/bench --split test --out test.json
quoth eval suite run eval/suites/bench.json --fixture-dir /tmp/bench --out /tmp/bench-run
```

Without model files, `--model hash-ngram` builds the same fixture for a plumbing check
(`tests/test_bench_set.py` does this); its numbers say nothing about quality.

**Composition.** 539 rows: 416 answerable, 123 abstain. Questions were written natively in
each language, not translated; 128 rows ask in the other language than the note's (tag
`xlang`), as a bilingual owner does.

| Rows | dev en | dev ru | test en | test ru | all |
|---|---|---|---|---|---|
| answerable, one note (any-of) | 80 | 75 | 107 | 107 | 369 |
| answerable, all-of `groups` (2–3 notes) | 11 | 7 | 20 | 9 | 47 |
| abstain `not_in_vault` (plausible, absent) | 7 | 12 | 14 | 15 | 48 |
| abstain `false_premise` (a note contradicts it) | 9 | 6 | 8 | 12 | 35 |
| abstain `near_miss` (a look-alike exists, does not answer) | 11 | 9 | 14 | 6 | 40 |
| **total** | 118 | 109 | 163 | 149 | 539 |

- 78 answerable rows carry `negatives` (look-alike notes that must not be injected: the other
  trip, the 2025 plan when the question asks about now); every `near_miss` row names its
  look-alike too.
- 44 group rows need 2 notes, 3 need 3 (`hops` = groups).
- 41 rows are paraphrases of another row (`paraphrase_of`): the same need as a keyword query,
  a vague or elliptical one, or in the other language.
- Styles (tag `style:*`): 453 natural, 31 keyword, 18 temporal ("last March", "позапрошлым
  летом"), 15 vague, 12 elliptical follow-ups with `context`, 10 entity lookups.
- Kinds: 267 fact, 32 temporal, 30 episodic, 28 person, 20 preference, 13 knowledge_update,
  12 followup, 8 decision, 6 project_state, 123 unanswerable.

**Splits.** By cluster: 25 note clusters, each wholly in `dev` or `test`, and no note is a
source or a negative in both splits (rows that would bridge them were left out). `dev` holds
the recipes (breads, soups), running and health, money, the flat, transport, Tidepool, the
garden plot, the piano and the new-year/Dresden/organ journal entries; `test` holds the
trips, people, work, books, Portuguese, the cat, the balcony plants, weeknight cooking and
the laptop. The split is by topic, so dev and test differ in content, not only in rows; tune
thresholds on `dev`, report `test`. `eval/suites/bench.json` runs `test` as `quality`, `dev`
as `tune` and the no-memory set as `regression`.

**Provenance.** Vault and questions written by a language model (`provenance.author:
llm:claude-opus-5.5`, 2026-11-01), `origin: synthetic`. Blind check: a second instance of the
same model, in a fresh context and without the labels, read the whole vault and answered each
question (shuffled, under neutral ids) with the notes that answer it or "none". Agreement with
the original labels: **538 of 539 rows (99.8%)** (abstain rows: checker says none; answerable
rows: the checker names a labelled note, one per group for all-of rows); the checker named
exactly the labelled notes on 522 (96.8%). Adjudication changed 11 rows, recorded as `review:
edited`:

- 1 disagreement, the checker was right: a "router password" question had been labelled
  `near_miss`, but the note says where the password is; now answerable.
- 6 rows gained a note the checker found that also answers them (any-of sources or a group
  alternative), e.g. Frau Möller also holds a key to the flat.
- 4 rows reworded after the checker flagged them ambiguous (Easter fell in the Porto trip's
  week; a combined vet bill; the 2025 emergency-fund target; the enclosure vs the radiation
  shield).

The other 528 rows are `review: accepted`. Every row has `reviewed_by:
["llm:claude-opus-5.5:blind-checker"]`. `SPOTCHECK.md` lists 40 rows (seeded, stratified by row
type and language) for the owner's review; the set becomes v2 of this card once they are read.

**Baseline** (quoth after 0.17.1, with the unreleased short-message rule at its tuned default; BGE-M3, dense retrieval, the serving
gate and evidence budget, 4 CPUs, 2026-11-01). Rates over answerable rows unless said; 95%
Wilson intervals in brackets.

| Metric | test (312) | dev (227) | all (539) |
|---|---|---|---|
| vault hit@1 | 0.914 | 0.821 | 0.875 |
| vault hit@5 | 0.979 [0.953–0.991] | 0.942 | 0.964 [0.941–0.978] |
| vault hit@10 | 0.992 [0.971–0.998] | 0.983 | 0.988 [0.972–0.995] |
| vault MRR | 0.946 | 0.875 | 0.916 |
| injected hit rate | 0.696 [0.635–0.750] | 0.694 | 0.695 [0.649–0.737] |
| injected precision | 0.852 | 0.784 | 0.823 |
| groups all@10 / injected all | 1.000 / 0.379 (29) | 0.833 / 0.278 (18) | 0.936 / 0.340 (47) |
| negative leak rate (answerable rows with negatives) | 0.367 | 0.500 | 0.432 |
| abstain non-empty rate | 0.420 [0.311–0.538] | 0.500 | 0.455 [0.370–0.543] |
| — `not_in_vault` | 0.103 (29) | 0.211 (19) | 0.146 (48) |
| — `false_premise` | 0.600 (20) | 0.733 (15) | 0.657 (35) |
| — `near_miss` | 0.700 (20) | 0.600 (20) | 0.650 (40) |
| p95 prefetch latency | 44 ms | 48 ms | 46 ms |

By language (all rows): English hit@5 0.977, MRR 0.935, injected hit 0.729; Russian hit@5
0.950, MRR 0.896, injected hit 0.657. Abstain non-empty: English 0.508, Russian 0.400.

**What it can detect.** With 10% of rows differing between two runs, a paired McNemar test
detects about 5.7 points of injected hit rate on the test split (243 answerable rows), 4.3 on
the whole set (416), and about 10 points of abstain injection on test (69 abstain rows). Two
gate configurations that differ on fewer rows give a smaller `mde` (`eval compare` reports
it). Example: the first draft of the short-message rule (up to 2 content words need a shared
word or cosine 0.65) changed 15 of 416 answerable rows here: injected hit rate 0.695 → 0.663
(14 lost, 1 gained, exact p 0.001), while abstain injections fell only 0.463 → 0.439. 9 of the
14 lost rows were Russian questions about English notes or the reverse: a short cross-language
question shares no word with its note. Tuned on the dev split (1 content word, cosine 0.60),
the rule costs nothing measurable here (0.695 → 0.695) and still cuts false injection on the
no-memory set.

**Limits.**
- A 72-note vault is small: vault hit@10 sits near the ceiling with BGE-M3, so a change in
  candidate retrieval shows up in hit@1 and MRR, not hit@10. The set measures the gate (what
  is injected, what is held back) far better than recall at depth.
- One author: questions and notes come from the same language model, so their wording is
  closer than a real person's notes and questions are. The blind check was the same model in
  a fresh context, not a different model family or a person; it catches wrong labels, not a
  shared blind spot. The owner's spot check (`SPOTCHECK.md`, 40 rows) is the human review.
- `false_premise` rows count any injection as false, but injecting the note that contradicts
  the premise can help an answerer correct it. Read that reason's rate as "how often the
  gate fires on a contradicted question", not as harm.
- `context` (follow-ups) and `asked_at` are recorded but `eval run` ignores them: a
  follow-up is retrieved on its own words, as the sidecar does today.
- Temporal questions ("last March", "this August") assume 2026-11-01 (`asked_at`).
- The split is by topic, so the two splits are not equally hard (dev hit@1 0.82, test 0.91):
  compare a change on the same split, never dev against test.
- Old and new versions of a plan are judged `negatives`, not `stale`: an old note in the block
  next to the new one is a precision loss, not a wrong answer, and `stale` would fail every
  suite comparison on it.

**Version history.** v1 (2026-11-01): first version; 72 notes, 539 rows, blind-checked,
owner spot check pending.
