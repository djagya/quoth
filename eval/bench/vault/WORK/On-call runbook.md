---
tags: [work, oncall]
---

# On-call runbook

## Rotation

Weekly, handover on Monday at 10:00 in the platform channel. Six people in the rotation, so
on call roughly every six weeks. Compensation: a flat 300 € per week on call.

## Escalation

Primary → secondary after 15 minutes → Hanne after 30 minutes.

## Kestrel (the routing service)

- Restart: `kubectl rollout restart deploy/kestrel-router -n routing`.
- If the queue backlog passes 50,000 messages, scale the consumers to 12 replicas before
  anything else.
- Dashboards: "Kestrel overview" and "Queue depth".

## Billing

Since Ledgerline, billing pages go to the payments team, not to platform on call.
