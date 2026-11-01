---
created: 2026-07-08
tags: [work, incident]
---

# Kestrel queue backlog, 8 July 2026

After the 14:00 deploy, the Kestrel routing queue grew to 180,000 messages. Route updates to
drivers were delayed by up to 1 hour 25 minutes.

- Cause: a memory leak in the new consumer build; pods were OOM-killed and restarted in a
  loop.
- Mitigation: scaled consumers to 16 replicas, then rolled back the deploy at 15:10.
- I was primary on call and ran the incident.
- Follow-up: memory limits alert, and a canary step for consumer deploys.
