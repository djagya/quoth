---
created: 2026-09-20
tags: [project, plan]
status: active
---

# Tidepool — v2 plan

Replaces the v1 plan, September 2026.

- Radio: LoRa at 868 MHz instead of Wi-Fi, so a second station can sit on the garden plot
  (about 2 km away), where there is no Wi-Fi.
- New sensors: a tipping-bucket rain gauge and a cup anemometer for the garden station.
- Storage: drop InfluxDB; store readings in SQLite through a small Go server on the Pi.
- Keep Grafana for the dashboard.
- Budget: 140 €, parts ordered in October 2026.
- Target: both stations running by March 2027.
