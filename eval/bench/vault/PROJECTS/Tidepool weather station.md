---
title: Tidepool
tags: [project, hardware]
created: 2025-11-02
status: active
---

# Tidepool — balcony weather station

A small weather station on the balcony, my hardware side project since November 2025.

## Current setup

- ESP32 board with a BME280 sensor (temperature, humidity, pressure).
- Sends a reading every 5 minutes over Wi-Fi to the Raspberry Pi 4 in the hallway (MQTT).
- Grafana dashboard on the Pi, shared with Mum, who checks Leipzig weather on it.
- Power: one 18650 cell and a 1 W solar panel; survives about 9 days without sun.

## Known problems

- In direct sun the sensor read up to 2 °C too warm. Fixed in April 2026 with a
  3D-printed radiation shield (stacked plates, white PETG).
- Wi-Fi drops in deep winter when the battery is cold.
