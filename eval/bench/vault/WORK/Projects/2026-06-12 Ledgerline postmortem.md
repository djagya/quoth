---
created: 2026-06-12
tags: [work, incident, postmortem]
---

# Ledgerline cutover postmortem

On 2 June 2026, 47 minutes after the write cutover, customers started receiving duplicate
invoices. 312 invoices were sent twice before we paused the sender.

## Root cause

The retry worker re-sent failed requests without the original idempotency key, so the new
ledger treated each retry as a new invoice. The old system deduplicated by order number,
which hid the bug for years.

## What went well

Reconciliation caught it within the hour; the pause switch worked.

## Action items

- Carry the idempotency key through the retry worker (Sanna, done 9 June).
- A unique constraint on (customer, order, period) in Postgres (Piet, done).
- A game day for the next cutover (me, planned for Q3).
- Apology credit of 10 € to the affected customers (finance).
