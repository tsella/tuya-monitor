# tuya-monitor

**Your Tuya sensors, off the vendor cloud and into a database you own.** Local polling over
the LAN protocol, one SQLite file per device, graphs on demand, alerts when a reading goes
wrong — no IoT Platform project, no subscription, no cloud round-trip per sample.

[![tests](https://github.com/tsella/tuya-monitor/actions/workflows/tests.yml/badge.svg)](https://github.com/tsella/tuya-monitor/actions/workflows/tests.yml)
![python](https://img.shields.io/badge/python-3.12%2B-3d7a80)
![license](https://img.shields.io/badge/license-MIT-2f6f4f)
![runtime](https://img.shields.io/badge/cloud--free_at_runtime-5cc8ff)
![protocol](https://img.shields.io/badge/local_6668-v3.3%E2%80%933.5-6b7280)

## Why

- **The cloud is used once.** Pairing borrows the app's own API to fetch each device's
  `localKey`; after that every sample comes off port 6668. Cut your internet and collection
  keeps running.
- **History you can query.** Per-device SQLite, WAL, `(ts, dp)` primary key, `INSERT OR
  IGNORE`, so a retried poll can never double-count. Retention is yours: capped or forever.
- **Alerts that don't nag.** Static watermarks per datapoint plus a robust same-hour
  baseline; one alert per excursion via cooldown, written to the DB before it is ever sent.

## Quickstart

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.toml ~/.config/tuya-monitor/config.toml && chmod 600 ~/.config/tuya-monitor/config.toml
./scripts/install.sh --dry-run && ./scripts/install.sh

.venv/bin/python -m monitor.cli pair          # QR in the terminal; approve in the app
.venv/bin/python -m monitor.cli verify <dev> # proves the key over 3.3→3.5, then enables
```

First chart:

```bash
curl -H "X-API-Key: $KEY" "localhost:8765/devices/<dev>/graph.png?start=$(date +%s -d -1h)&end=$(date +%s)" -o chart.png
```

## Features

- one poller thread per device, exponential backoff, auto-disable after 5 failures with a
  `device_offline` alert
- `X-API-Key` on every route, keys masked in responses, loopback by default
- PNG charts with labelled limits, a `°C`/`°F` dual axis, min–max bands per bucket
- daily and weekly chart packs, a 15-minute alert digest that stays `[SILENT]` when quiet
- pairing imports land **disabled** until `verify` proves the key — no silent garbage

## API

`GET /devices` · `POST /devices/{id}/verify` · `POST /devices/{id}/poll` ·
`GET /devices/{id}/latest` · `GET /devices/{id}/series?start&end&bucket` ·
`GET /devices/{id}/graph.png` · `POST /pair` · `GET /pair/{session}` ·
`GET /alerts` · `POST /alerts/{id}/ack` · `GET /healthz`

Epoch seconds; `bucket` returns min/max/avg windows; raw ranges cap at 90 days.

## Docs

`docs/adr/0001-architecture.md` (design and failure modes) · `docs/schema.md` ·
`docs/state-machines.md` (invariants and the bugs found against them) · `docs/runbook.md` ·
`skills/tuya-pair-and-monitor/SKILL.md` (the whole procedure, written for agents).

## Scope

Built for one household and the `co2bj` sensor family; datapoint metadata is a dict in
`monitor/config.py`, so another Tuya category is an entry, not a rewrite. Tuya's app
constants live in `monitor/config.py` and are re-extractable when they rotate — see the skill.

MIT.
