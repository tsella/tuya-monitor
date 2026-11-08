# ADR 0001 — Architecture of tuya-monitor

Status: accepted · 2026-11-08

## Problem

A Tuya CO₂ sensor (`co2bj`, devId `<device-id>`, `<lan-ip>`) speaks the local
protocol on 6668. We want continuous history, an API to pair further devices, graphs over
arbitrary ranges, scheduled daily/weekly plots, and alerts when readings go wrong — with
no cloud dependency at read time.

## Decisions

**One SQLite file per device.** `data/<dev_id>.db`, WAL, `PRIMARY KEY (ts, dp)`
`WITHOUT ROWID`. Reasons: a lost/corrupt device file is a single-device loss instead of a
fleet-wide one; retention and `VACUUM` run per device without locking others; a device
that fires 5 Hz cannot slow the registry. The registry (`data/registry.db`) holds device
identity, key material, pairing sessions and alerts — it is small, hot, and writable.

Consequence: cross-device queries are a fan-out in app code. Acceptable at house scale;
if that changes, a rollup DB is the escape hatch, not a schema rewrite.

**Key material never lands in telemetry DBs.** `registry.db` holds `local_key`, chmod
600, and the whole `data/` tree is 700. A copy of a chart DB leaks nothing.

**Collector is a supervisor, not a loop.** One goroutine-ish thread per enabled device,
each with its own connection and exponential backoff (0.5 s → 60 s cap, jitter), plus a
`last_error`/`consecutive_failures` counter persisted in the registry. A flapping device
does not stall the others and the failure is visible through `/devices` instead of only
in logs.

**Anomalies are computed at write time, not query time.** Detection needs the baseline
that exists *now*; back-filling decisions later would rewrite history. Rules are:

- static bounds per DP kind (CO₂ warn 1000 / alert 1500 ppm; temp 5–35 °C; RH 20–80 %)
- robust dynamic bounds: same-hour band over the last 7 days, median ± 4·MAD, needing
  ≥12 baseline points, floor-capped so a quiet device cannot alert on noise
- per-(device, dp, rule) cooldown of 30 min, so one excursion is one alert, not 200

Every alert is written to `alerts` even when notification fails. Notification is a
separate cron reader, so a Telegram outage cannot lose data.

**Graphs are rendered on demand from SQL, never stored as the source of truth.** PNGs in
`reports/` are artefacts; they are pruned after 30 days. Bucketing is done in the query
(`strftime`-aligned) so memory stays bounded for long ranges.

**API is localhost-bound with an optional bearer token.** It can write (pair, poll-now,
ack), so the defaults are `127.0.0.1:8765` and `api_key` in the config; no TLS, no CORS.
A reverse proxy is the way to expose it.

**Pairing copies the cloud path from the `tuya-app-api-local-key` skill** (QR token →
approve → `location.list` → `group.device.list` with `gid` as a query param). The local
key is verified against the device (3.3 → 3.4 → 3.5 sweep) *before* the device row goes
`enabled=1`; a key that fails every version is stored but disabled, so the UI shows a
device that exists and does not silently collect garbage.

## Failure modes considered

| Failure | Behaviour |
|---|---|
| Device offline / Wi-Fi drop | per-device backoff, `last_error`, no data loss elsewhere |
| Key rotated by re-pair | 914 on every poll; row disabled after 5 failures + alert |
| Clock jump / duplicate ts | `INSERT OR IGNORE` on `(ts, dp)` |
| Disk full | collector logs and stops the device, API still answers reads |
| App API changes | pairing breaks only; local collection unaffected |
| Alert storm | cooldown + severity gate; cron sends a digest, not per-row |

## Rejected

- **MQTT/InfluxDB**: more moving parts than a house needs; SQLite + files is greppable.
- **Wide sample table** (one column per DP): needs migrations per device category.
- **Storing the cloud `sid` only**: it expires in hours; local keys must be the durable
  artefact.
- **Rendering charts in the browser**: cron needs PNGs without a browser.
