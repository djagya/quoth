# no-memory: messages that need no recall

**What it measures.** How often prefetch injects something into a turn that needs no memory
at all: the false-injection rate (`abstain_nonempty_rate`, and per store
`abstain_injected_by_store` in a sweep). Every row abstains, with `abstain_reason:
no_memory_needed`.

**Why portable.** The rows are about nothing in anyone's vault, so the manifest's fixture is
`any`: a suite runs the set against the public fixture, a personal snapshot, or a live
sidecar. A vault that happens to hold a note on one of these topics (a trip to Peru, a note
on git) can make a row's injection correct; leave such a row out with a personal set rather
than editing this one.

**Composition.** 120 rows, 60 English and 60 Russian, written natively in each language (not
translated), in matching categories (tag `nm:<category>`):

| Category | Rows per language | Examples of the kind |
|---|---|---|
| `chitchat` | 10 | greetings, acknowledgements, "you there?" |
| `thanks` | 6 | closing a topic |
| `general` | 12 | general knowledge |
| `math` | 6 | arithmetic, unit conversion |
| `code` | 8 | programming how-tos |
| `writing` | 8 | rewrite, shorten or fix a text given in the message |
| `translate` | 5 | phrase translation |
| `meta` | 5 | questions about the assistant itself |

**Provenance.** Written by a language model (`provenance.author: llm:claude-opus-5.5`,
2026-10-31) and accepted by its author; no second human review yet. Treat it as v1.

**Limits.**
- Short messages ("ok", "lol") test the gate's behaviour on near-empty queries; the sidecar
  may skip them before retrieval, which is the correct outcome.
- The set alone rewards a gate that never injects. Read it next to an answerable set: a
  threshold change is good only if injected hits on answerable rows hold while this rate
  falls.
- With 120 rows the 95% interval of a rate near 0.5 is about ±9 points.

**Version history.** v1 (2026-10-31): first version.
