---
title: Ledgerline migration
tags: [work, project]
created: 2025-10-06
status: done
---

# Ledgerline migration

Moving Fernhollow's billing ledger off the legacy MySQL 5.7 cluster onto Postgres 16.
I own the project; Hanne is the sponsor, Piet handles the database side, Sanna writes the
reconciliation jobs.

## Plan

1. Dual write behind the feature flag `ledger_dual_write` (from November 2025).
2. Nightly reconciliation of both ledgers; any difference over 0.01 € pages the team.
3. Switch reads to Postgres per customer segment, smallest first.
4. Cutover of writes.

## Dates

The first target was the end of Q1 2026; it moved to 30 May 2026 because of VAT rounding
differences between the two systems (the old one rounded per line, the new one per invoice).
The cutover finally happened on 2 June 2026.

## Status

Done. Postgres has been the only writer since 2 June 2026. Remaining: the decommission of
the old cluster (separate note).
