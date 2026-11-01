---
tags: [tech, backup]
created: 2026-09-01
---

# Backup setup

Set up after the laptop disaster in August 2026.

- restic, nightly at 02:00 via a systemd timer, to an S3-compatible cloud bucket.
- Retention: 7 daily, 4 weekly, 12 monthly snapshots.
- Second copy: the external 2 TB SSD, plugged in on the first Sunday of each month.
- The restic repository password is in the password manager, under "restic laptop".
- Test a restore every quarter; first test on 1 October 2026 worked (restored the notes
  folder to /tmp).
- Not backed up: the Downloads folder and virtual machines.
