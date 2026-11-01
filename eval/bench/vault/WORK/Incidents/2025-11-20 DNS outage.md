---
created: 2025-11-20
tags: [work, incident]
---

# DNS outage, 20 November 2025

Partial outage for 2 hours 10 minutes. Internal service names failed to resolve after a
change to the cluster DNS config; services that cached addresses kept working, the rest
returned errors.

- Detected by customers before our alerts (bad).
- Fix: rolled back the config; added an alert on DNS error rate.
- Follow-up: DNS changes now need a second reviewer and go out on Tuesdays only.

I was secondary on call that week.
