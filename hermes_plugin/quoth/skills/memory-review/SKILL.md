---
name: memory-review
description: "Use when the owner asks to review memory, or answers a memory-review nudge: presort the Quoth queue yourself, then walk the owner through it topic by topic in short numbered rounds and apply exactly what they answer."
version: 3.3.0
author: quoth
---

# Memory review (Quoth ledger)

Long-term memory is the Quoth ledger. Nothing you propose is memory until
the owner accepts it here. You do the digging; the owner makes small, plain
decisions. The review runs in the owner's own chat, because every change
carries their exact words.

## When

- **Presort** (yours, no approval): before a review, or when an operator asks.
- **Review** (the owner decides): they say "review memory", or reply to the
  weekly nudge. Never start a review on your own in another conversation.

## 1. Presort (decide nothing)

1. `review_claims action=assist` (pages: `offset`, `limit` ≤ 50). It returns,
   per claim, `near` (close claims with cosine scores, pending or accepted),
   `vault` (the top note hit: `locator`, `score`, `excerpt`) and `clusters`
   of near-duplicates. If `complete` is false (out of time, or it gave way
   to a live chat turn), call again with the same page; `next_offset` is the
   next page.
2. Read the claims (`action=queue`) and record one `action=suggest id=N`
   each, with:
   - `suggestion`: `accept` (durable, true, about the owner), `reject` (wrong,
     one-off, not about them), `duplicate` (+ `duplicate_of` = the claim that
     stays), `covered` (their notes already say it: + `evidence_locator` from
     `vault`, only when the excerpt really says the same), `stale` (no longer
     true), `keep` (you truly cannot judge);
   - `topic`: a short label, at most 40 chars ("Food & health", "Work",
     "Family"). Use few topics, about 5 to 12, so each holds a round or two;
   - `kind` from the enum, always;
   - `text`: a clean rewrite (third person, one fact, under ~200 chars) when
     the claim is wordy. For a duplicate cluster, put the merged wording on
     the claim that stays (its `accept` suggestion) and point the others at it.
     A duplicate is only applied when the claim it points at stays (accepted,
     or suggested `accept`); never point two copies at each other;
   - `note`: a few words, only when the reason is not obvious.
3. Re-suggesting replaces the earlier suggestion; the topic is kept when you
   omit it. Prefer a clear `accept` or `reject` over `keep`.
4. Done when `action=digest` shows `plan.unsorted` = 0.

## 2. Open the review

Call `review_claims action=digest` and open with one short message:
progress and the plan, from `pending` and `plan`:
"N waiting. After clean-up about M to read, about K rounds of 10."

## 3. Bulk clean-up first

Offer the junk buckets in one message, with counts and two or three
examples each (`action=digest bucket=duplicate`, `covered`, `stale`,
`reject`): "Clear these? (ok / skip / show duplicates)". Mark a duplicate
whose `duplicate_target_kept` is false: it will not be applied.
On their yes, one `action=apply bucket=<name> bucket_stamp=<buckets.<name>.stamp
from that digest> approval_quote=<their reply>` per bucket. `bucket_changed`
means the bucket moved since they saw it: show it again. One reply decides at
most 200 claims; a bigger bucket goes by `ids`, a page per reply.

## 4. Topic rounds

`action=digest` returns the next topic's items (`group.items`, numbered
`n` from 1). Send 8 to 12 per message, each as one plain line:

    3. Prefers green tea. (remember)

`text` is already the rewrite. Tags: remember (accept), your call (keep),
duplicate (+ what stays: `duplicate_of_text`), in notes (covered),
outdated (stale), drop (reject). End with: "ok / ok, drop 3 5 / 3: new text
/ skip / stop".

The owner's reply grammar, applied exactly. Their numbers are item numbers `n`;
map each to its `id` from the same digest. The sidecar checks the reply
against this grammar: an assent ("ok", "yes", "да", "👍", "yes please",
"да, давай", "go ahead", "ok all"), optionally
", drop <numbers>", then any "N: text" edits (one per line, or after ";").
A reply with "not", "no", "нет", "не", "отмена" or similar outside an edit's
text approves nothing, and each part approves only its own decision: the
assent applies and accepts, "drop N" rejects those, "N: text" edits. When a
call fails with `approval_quote_not_found` or `approval_not_for_action`
("ok but not 3", "drop 2" for an apply), apply nothing and ask them to say
it in the grammar.

| The owner says | You do (`approval_quote` = their whole reply, verbatim) |
|---|---|
| "ok" | `action=apply ids=[ids of every item shown except "your call"]` |
| "ok, drop 3 5" | first `action=reject ids=[ids of 3 and 5]`, then `action=apply` for the rest |
| "3: new text" | `action=edit id=<id of 3> text=<new text> kind=<item kind>`; combine with ok/drop as said |
| "skip" | nothing; from now on pass `action=digest topic=<next topic in topics you have not skipped>` |
| "stop" | nothing; say what remains |
| "undo" (or "отмени", "верни") | `action=undo`: reverts what their message right before it decided |

Several calls for one reply share that reply as `approval_quote`, so one
"undo" reverts them all. Items with "your call" stay pending under "ok";
the owner keeps one with "3: <text>" (an edit) or drops it with "drop 3".
Anything the owner did not answer stays pending; do not guess. A topic with more
items than one message continues with `action=digest topic=<same>` until it
is empty.

After each reply, report one line: "Done: 7 remembered, 2 dropped, 1
edited. 41 left (about 5 rounds)." List refused ids with their error.
When a result lists `restored_duplicates`, add: "N duplicates came back
because their original was dropped" (they return to review). Then send
the next round.

## 5. After the review

When anything was accepted or edited, or an undo returned `core_stale: true`,
republish the core block with the quoth install's command:
`quoth ledger compile-core --publish "$QUOTH_HOME/core"`
(the ledger defaults to `$QUOTH_HOME/ledger.db`). New sessions read the new
core; say so.

`/quoth queue` in chat shows the same digest read-only; `/quoth queue
<topic>` lists one group; `/quoth item N` shows one memory in detail.

## Rules

- `approval_quote` is the owner's reply in this turn, copied verbatim and
  whole: never paraphrased, never an older message, never your words. It
  must be their newest message, with nothing after it but your tool calls. Apply,
  accept and reject need the whole reply in the grammar above; undo needs
  an undo word alone. Edit text and `forget` may quote the owner's own words
  (8 or more characters); `forget` never takes a bare "ok".
- Accepted claims are short, third-person facts about the owner that will
  still be true next month. One-off events, task status and what their
  notes already say are not memory.
- Nothing is deleted: rejected, superseded and undone claims stay on the
  record with their history; `forget` is reversible by re-proposing.
- Memory content stays in this chat. Do not copy claims into notes, cards,
  logs or other conversations.
