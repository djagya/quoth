# Core-memory ledger

The ledger is the assistant's mutable core memory: typed claims, reviewed by the owner,
compiled into a short block the assistant loads every session. It is its own SQLite file
(`$QUOTH_HOME/ledger.db`, schema `quoth-ledger/1`, migrations in
`src/quoth/ledger/migrations/`) beside the evidence snapshot. It never writes to the vault
or to a snapshot.

## Where a claim comes from

Every claim records its `origin`; recall and the core show the derived `authority`.

| `origin` | `authority` | Meaning |
|---|---|---|
| `owner_statement` | `owner` | the owner's own words in a chat message, quote-verified against Hermes `state.db` |
| `owner_note` | `owner` | something a note of the owner's says (`evidence_locator` = the vault path) |
| `assistant_proposal` | `assistant` | the assistant's inference; served only after the owner accepts it, and marked `[inferred]` in the core |
| `imported` | `imported` | text imported from another memory (`quoth ledger seed`, or Hermes built-in MEMORY/USER writes) |

The caller never chooses `owner_statement`: the sidecar sets it only when the quote is found.

**Quote verification** (`ledger/authored.py`) reads only `role='user'` rows of the calling
session (Hermes source `telegram`), active or compaction originals, after stripping runtime
wrappers, reply context, captions and attachments. Turns Hermes injected itself (scheduled
deliveries, notices, internal notifications) are never authored. The quote (8 to 500
characters) must appear in the authored text after NFC and whitespace normalization. The
claim's locator is `hermes:<session_id>:<message_id>`.

## Kinds and auto-accept

`identity`, `preference`, `consent`, `correction`, `environment`, `person`, `taste`,
`project_state`, `episode`, `pointer`. Imported text arrives as `unclassified` and must be
given a kind before it can be accepted.

A proposal is accepted without review only when all of these hold: origin
`owner_statement` with a verified quote, kind `identity`, `environment`, `person`,
`project_state` or `pointer`, and confidence ≥ 0.9. It stays undoable (`undo-auto`), which
also restores the value it replaced. Preferences, consent, corrections, taste, episodes and
every assistant inference always wait for the owner.

## Write-time reconcile

`propose` decides before inserting:

| Decision | When |
|---|---|
| `noop_duplicate` | the same text (case, whitespace and final punctuation folded) is already current; counted as a re-proposal |
| `replace_pending` | a pending claim with the same `subject_key` exists; it expires (latest wins) |
| `supersede_proposed` | the claim targets an accepted one (explicit `supersedes`, or the same `subject_key`); on acceptance the old claim gets `valid_to` and `superseded_by` |
| `add` | otherwise |

The response lists up to 3 `closest` current claims (word overlap ≥ 0.35), so the proposer
or the reviewer can supersede or merge instead of duplicating.

## Nothing is deleted

Claims are bitemporal (`valid_from`, `valid_to`, `recorded_at`). Superseding, forgetting
and expiring set fields; triggers refuse `DELETE` on claims, any change to a claim's
content, and any update or delete of `ledger_events`. An edit creates a new accepted claim
that supersedes the old one. Every decision is appended to `ledger_events`.

## Presort suggestions

`claim_suggestions` holds non-binding suggestions per pending claim: `accept` (optionally
with a kind and a cleaner rewrite), `reject`, `duplicate` (of a current claim), `stale`,
`keep`, or `covered` (the owner's notes already say it; carries the vault
`evidence_locator`). A suggestion may carry a `topic` (up to 40 characters). The latest
suggestion per claim wins; a re-suggestion without a topic keeps the previous topic.
Suggestions never change a claim.

The assistant records them with `review_claims action=suggest` (no approval needed). The
owner confirms a batch with `action=apply`: accept becomes an accept (or an edit when a kind
or rewrite is suggested); reject, stale, duplicate and covered become rejects with the
reason in the review note; keep stays pending.

## Guided review

The `memory-review` skill has the assistant do the sorting and the owner answer in a small
grammar: "ok", "ok, drop 3 5", "3: new text", "skip", "stop", "undo".

**Approvals are bound to the turn.** Every decision (`apply`, `accept`, `reject`, `edit`,
`forget`, `undo_auto`, `undo`) needs `approval_quote` from the owner's message that opened
the current turn: the session's newest user row must be owner-authored (an injected row
makes it fail), with no completed assistant reply after it. What that message must say:

| Decision | The message must be |
|---|---|
| `apply`, `accept`, `reject` | the whole reply, parsing as the grammar: an assent word ("ok", "yes", "go ahead", 👍, …), optionally ", drop N …", then any "N: text" edits. A negation anywhere outside an edit's text ("no", "not", "don't", "stop", "skip", …) approves nothing. `apply` and `accept` need the assent; `reject` needs "drop N" with no more ids than numbers; a reply of only "N: text" edits approves only edits |
| `undo` | an undo word alone ("undo", …) |
| `edit`, `forget`, `undo_auto` | a quote of 8 to 500 characters from the message, or a short whole reply (an assent for edit, an undo word for undo_auto) |

Assent, drop, negation and undo words are listed in `ledger/authored.py` (English and
Russian). Two turn boundaries fail closed: an assistant text row written in the middle of a
turn makes later decisions in that turn stale, and a compaction in the middle of a turn
re-inserts the owner's row under a new id, so an `undo` afterwards cannot reach decisions
made before it.

**Presort assist** (`action=assist`, read-only): near-duplicate clusters among pending and
accepted claims (BGE-M3 cosine ≥ 0.85) and, per claim, its top vault hit (locator, score,
a 200-character excerpt). One call does at most 8 seconds of work, one assist runs at a
time (`assist_busy`), and it yields (`complete: false, yielded: true`) when another
session's turn queried in the last 20 seconds. `next_offset` pages through the queue.

**Digest** (`action=digest`, read-only): pending counts by suggestion and by topic, the plan
(`cleanup` = duplicate + covered + stale + reject + dropped_copies, `to_read`, `unsorted`,
rounds of 10), each junk bucket's `count` and `stamp`, and one group of numbered items
(`topic=…`, `bucket=…`, or by default the first topic with something to read).

**Duplicates fold into a claim that stays.** Applying a `duplicate` needs its target to be
accepted and current, or pending with a current `accept` suggestion; otherwise it stays pending
(`duplicate_target_not_kept`).

**Dropping a claim drops its confirmed copies.** When the owner rejects a claim (`reject`, or
`apply` of a junk suggestion), its family goes with it in the same transaction and under the
same approval, with no extra review round. The family is every claim confirmed as a copy of it,
transitively: a claim whose latest decision is a rejection as a duplicate of a family member.
Copies already rejected stay as they are, with no new event. A copy that is pending only
because the system restored it (`restored` with `target_dropped` in the family, and no decision
by the owner since) is rejected again: a `rejected` event with `duplicate_of` (its original
target), `family_of` (the dropped claim) and the same `batch`, so `undo` returns it to pending
together with the claim. A claim with only a pending `duplicate` suggestion, a copy the owner
decided again after a restore (accepted, edited, undone, or dropped for another reason) and
every accepted claim are left alone. Each reject result carries `family: {root, ids,
rejected_now, already_rejected}`, and the re-rejects count toward the approval cap. A repeat
of the same reject is refused per id (`not_pending`) and writes nothing.

**Expiry restores copies.** When a pending original ends without the owner's decision (expired
by age, or replaced by a newer proposal for its slot, latest-wins), its rejected copies return
to pending in the same transaction (`restored`, `target_dropped`): the owner confirmed them as
the same fact, never that the fact is wrong, and nothing else would keep it.

**Copies of a memory the owner already dropped.** Releases before 0.18.1 restored an
original's confirmed copies to pending when the owner dropped the original. Such a copy is
pending, its latest decision is `restored` with `target_dropped` = X, X's latest decision is
the owner's `rejected`, and the owner has decided nothing about the copy since. These copies
form the `dropped_copies` junk bucket (`ledger.dropped_copies`), whatever their suggestion,
and the other buckets leave them out, so `cleanup` counts each claim once. Copies restored by
expiry by age or latest-wins are not in it: X then ended as `expired_age` or
`expired_latest_wins`, without the owner's decision. Neither are copies the owner decided
again, nor copies of an X whose drop was undone. They need no presort suggestion: the owner
confirmed them when they first dropped them as duplicates. Applying the bucket rejects each
as a duplicate of its original (`rejected`, `duplicate_of` = X, the approval's `batch`), so
`undo` returns them to pending; after an undo they are the owner's to review in the topic
rounds, no longer in the bucket. `digest bucket=dropped_copies` lists them with `copy_of`
and `copy_of_text` (the dropped original). Ledger health reports the count (`dropped_copies`).

**Bucket apply** (`action=apply bucket=duplicate|covered|stale|reject|dropped_copies
bucket_stamp=…`) applies exactly the bucket the owner saw: the stamp must still match
(`bucket_changed`), a bucket over 200 is refused (`bucket_too_large`), and buckets that add
memory (`accept`, `keep`) cannot be applied blind. A bigger bucket goes by `ids`, a page per
reply; for dropped copies that is `action=apply bucket=dropped_copies ids=[…]`, and an id
that is no longer such a copy is refused (`not_a_dropped_copy`). One approving message
decides at most 200 claims (`approval_cap_reached`), family re-rejects included.

**Batches and undo.** Every decision records its batch, the locator of the approving
message. `action=undo` reverts only the batch of the owner's message right before the
"undo", so one reply that led to several calls is undone as a whole. Rejected and accepted
claims return to pending (and what an accept retired is restored); a claim an edit created is
rejected and the claim it replaced returns to review. A batch holding a `forget` or
`undo_auto` is refused (`not_undoable`); a claim changed since is left alone
(`changed_since`); a second undo finds `nothing_to_undo`; an undone message cannot approve
again (`approval_spent`). When accepts or edits were reverted the reply says
`core_stale: true`.

**The sidecar republishes the core.** With `--core-dir`, every review decision that changes
accepted memory (`accept`, `edit`, `forget`, `undo_auto`, an `apply` that accepts, an `undo`
with `core_stale`) ends with `publish_core` under the sidecar's ledger lock, at the budget
`/v1/core` serves. The reply carries `core: {"published": <sha256>, "claims": <n>}`, or
`core: {"error": "publish_failed", "reason": <exception class>}` when publishing failed; the
decision stands either way. Rejects and reads publish nothing. Health shows the last attempt
(`core_publish`). Without `--core-dir` the core is compiled live and nothing is published.

## Serving

Recall returns accepted, current claims only; pending proposals are never served. The
sidecar ranks accepted claims by meaning (`claim_index.py` keeps their BGE-M3 vectors in
memory) and puts claims at cosine ≥ 0.5 first, then word matches.

`compile-core` renders accepted claims of the core kinds (episodes excluded) by section,
most used first, up to `budget × (1 − headroom)` characters (default 4000 × 0.8).
`--publish DIR` writes immutable `core-<sha12>.md` and `.json` (mode 0444), atomically
repoints `core.md` and `core.json`, and records the version in `core_versions`. The sidecar
serves `core.md` from `--core-dir` and republishes it after a review changes accepted memory.

## Health

`quoth ledger health` is red (exit 1) when more than 40 claims are pending or the oldest is
older than 14 days (`--max-pending`, `--max-age-days`). It also reports pending by kind, the
pending copies of dropped memories (`dropped_copies`), and 7-day counts of proposed,
re-proposed, accepted, auto-accepted, rejected, superseded, expired, forgotten and undone
claims. The sidecar's `/v1/health` uses the same rule.

**Core freshness.** With `--core-dir DIR` (and in the sidecar whenever it runs with
`--core-dir`), health compiles the core in memory, writing nothing, and compares it with the
published manifest (`DIR/core.json`):

```json
"core": {"status": "red", "reason": "core_stale", "published_sha": "<sha256>",
         "missing": 1, "extra": 0, "compiled_at": "2026-11-06T09:00:00.000Z"}
```

`missing` counts claims the core would carry now that the published core neither carries
nor left out for its budget (an accept or edit since it was published); `extra` counts claims
it carries that are no longer accepted and current (a forget, an edit, an undone accept).
Counts only, never claim text. The comparison is by claim id, not by the core's sha256:
injected claims gain `use_count`, which reorders the core without any review. A red core
adds its reason to `reasons` and turns the whole check red:

| Reason | When |
|---|---|
| `core_stale` | `missing` or `extra` is above 0 |
| `core_unpublished` | nothing is published in `DIR` while accepted core claims exist |
| `core_unreadable` | `DIR/core.json` cannot be read as a manifest |

The budget compared is `--core-budget` (in the sidecar, the budget it serves), else the
published manifest's. Publishing again (`compile-core --publish DIR`, or a review through
the sidecar that changes accepted memory) turns it green.

## Proposal audit

`deploy/diagnostics/quoth-proposal-audit.py` measures where rejected proposals come from. It
uses only the standard library (python3 ≥ 3.9), opens the ledger with `mode=ro`, and prints
one JSON document with counts only: no claim text, quotes, notes, locators or ids. Proposer
names that are not plain labels print as `other`.

```bash
python3 quoth-proposal-audit.py "$QUOTH_HOME/ledger.db" [--days 30] [--now ISO] [--near 0.8]
```

For all time and for the last `--days` (by proposal time) it reports each proposal's outcome
(`accepted`, `accepted_edited`, `rejected`, `duplicate_rejected`, `superseded`, `expired`,
`pending`) and `rejection_share` (rejections / owner decisions, auto-accepts excluded),
broken down by origin, proposer, kind at proposal and at decision, `quote_verified`, presort
suggestion and who made it, mirror action (`memory:add`, `user:replace`, …), text length
(the `600 (cap)` bin is text the mirror truncated) and coarse text-shape flags (path, URL,
code, first person, …). It also gives time to decision, decision batch size, how rejections
were made (`applied_suggestion`, `direct`, `family_of_dropped_claim`), presort precision
and recall against the owner's final decision, proposals of a text or `subject_key` already
rejected, and how many rejected claims repeat an earlier rejected text (exact or with word
overlap ≥ `--near`). `health`'s 7-day counts are events, so a claim rejected, undone and
rejected again counts twice there; this audit counts claims.

## Commands

`quoth ledger [--ledger PATH] ACTION`; the ledger defaults to `$QUOTH_HOME/ledger.db`.
Claim records and reports print as JSON.

| Action | What it does |
|---|---|
| `propose --text T --kind K --origin O [--quote Q --locator hermes:S:M] [--subject-key K] [--supersedes ID] [--confidence X] [--reason R] [--proposer P] [--state-db PATH]` | record a pending claim (an `owner_statement` quote is verified against `--state-db`, default `$HERMES_HOME/state.db`) |
| `queue [--limit 20]` | oldest pending claims |
| `accept ID [--note N]`, `reject ID [--note N]` | decide one claim |
| `edit ID [--text T] [--kind K] [--note N]` | replace a claim with new wording (accepted) |
| `forget ID --reason R` | end an accepted claim's validity |
| `undo-auto ID` | undo an auto-accept |
| `expire [--days 30] [--dry-run]` | expire pending claims older than N days |
| `health [--max-pending 40] [--max-age-days 14] [--core-dir DIR] [--core-budget N]` | queue size, age and churn; with `--core-dir`, whether the published core is stale |
| `seed --file FILE` | import JSONL `{"text", "kind"?, "reason"?}` as pending `imported` claims |
| `nudge` | print one review reminder when claims wait, nothing otherwise (read-only) |
| `compile-core [--budget 4000] [--headroom 0.2] [--publish DIR]` | print, or publish, the core block |

Exit codes: 0 ok, 1 runtime failure or a red health check, 2 usage error, 3 the ledger
refused the action (`{"error": code}` on stderr).

## Measuring knowledge updates

The public eval fixture seeds update chains (`eval/public/ledger-seed.jsonl`), and its
`knowledge_update` rows require the newest value to be served and no superseded value to be
injected (`stores.claims.hit@1`, `stale_leak`). See [EVAL.md](EVAL.md).

The Hermes side (tools, routes, the review skill) is in [HERMES.md](HERMES.md).
