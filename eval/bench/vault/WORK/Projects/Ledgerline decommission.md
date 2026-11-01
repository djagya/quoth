---
tags: [work, project]
created: 2026-09-15
status: active
---

# Ledgerline decommission

The old MySQL billing cluster is switched off on 15 December 2026.

- Until then it stays read-only; finance can still query it through the reporting replica.
- Billing records must be kept for ten years (tax law), so a full export goes to the cold
  archive bucket before shutdown, encrypted, with a checksum list.
- Piet runs the export dry run in November; I sign off.
- Saves about 2,300 € a month in hosting.
