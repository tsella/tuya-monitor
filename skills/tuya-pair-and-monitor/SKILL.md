---
name: tuya-pair-and-monitor
description: Use when pairing a Tuya device to this repo or standing up monitoring.
version: 1.0.0
---

# Pair a Tuya device, then turn monitoring on

Two phases. Phase 1 gets a `localKey` for one device over the app API; phase 2 makes the
service collect, chart and alert. Nothing here needs an IoT Platform project, root, or the
vendor app beyond the phone that is already signed in.

Read `docs/adr/0001-architecture.md` first if you are changing anything — the storage split
and failure model are deliberate.

## Phase 1 — pair

Prerequisites: the phone has the Tuya Smart app, logged into the account the device belongs
to, and the device is on the same LAN as this host. The app identity in `monitor/config.py`
must match the installed app build (see "When the cloud rejects signatures").

1. `.venv/bin/python -m monitor.cli init` — creates `data/` (700) and the registry.
2. `.venv/bin/python -m monitor.cli pair`
   It prints a QR path and waits. Scan it from **Me → the scan icon** in the app and approve.
   The CLI polls `tuya.m.user.qr.token.user.get` every 4 s, then imports everything the
   account exposes, **disabled**.
3. `.venv/bin/python -m monitor.cli devices` — confirm the row, its IP and category.
4. `.venv/bin/python -m monitor.cli verify <dev_id>` — sweeps protocols 3.3 → 3.4 → 3.5 and
   enables the device on the first that returns `dps`. Output looks like
   `ok at v3.5: {"2": 457, "18": 26, "19": 34, "101": "f"}`.
5. Repeat per device. Re-running `pair` for an already-known device refreshes identity and
   **keeps** `enabled`, `version` and `dp_meta`; it does not disturb collection.

### Sign contract (only if you touch `monitor/cloud.py`)

`POST` to `https://a1.tuyaus.com/api.json`, all parameters in the urlencoded body. The sign
covers sorted `k=v` pairs joined with `||`, restricted to the `SIGN_KEYS` set, with
`postData` replaced by a diced MD5 (`h[8:16]+h[0:8]+h[24:32]+h[16:24]`). `sign` is
HMAC-SHA256 hexdigest keyed `f"{certSha256}_{bmpSecret}_{appSecret}"`. Fixed parameters:
`v=1.0` (API version — the app version goes in `appVersion`), `os=Android`, `ttid=tuya`,
`lang=en_US`, a persistent random `deviceId`, and a fresh `requestId` per call.

Wrong sign → `SING_VALIDATE_FALED_4`. Wrong `v` → `API_OR_API_VERSION_WRONG`.

### When the cloud rejects signatures

The constants are per app build. Re-extract from the APK: `clientId`/`appSecret` from
`com/smart/app/SmartApplication.initKey` (androguard, not jadx — jadx OOMs on this app), the
BMP secret from `assets/t_s.bmp` with the matrix solver (`fixed_key.bmp` is a decoy and fails
with `invalid keys_cnt`), and the cert SHA-256 from `apksigner verify --print-certs`
(format it upper-case with colons; never use the source-stamp cert).

## Phase 2 — monitoring

1. `cp config.example.toml ~/.config/tuya-monitor/config.toml && chmod 600` it, then set
   `api_key` (`openssl rand -hex 24`).
2. Tune in the same file: `[thresholds.*]` watermarks, `temp_unit`, `poll_interval`,
   `retention_days` (0 = keep everything), `bind`/`port`. Units are the DP's own.
3. `./scripts/install.sh --dry-run` then `./scripts/install.sh`. Two user units start:
   `tuya-monitor-api` (127.0.0.1:8765) and `tuya-monitor-collect`.
4. `.venv/bin/python -m monitor.cli report daily` — proves the render path writes a PNG.
5. Schedule the deliverables (not the collection): a daily `report daily`, a weekly
   `report weekly`, and `monitor.cli notify` every 15 minutes which prints `[SILENT]` when
   quiet.
6. Smoke-test the alert path by inserting an out-of-range value and taking it all the way to
   `POST /alerts/{id}/ack`.

## Verifying without guessing

- health: `curl -H "X-API-Key: $K" localhost:8765/healthz`, and `401` on the same URL without it
- live data: `POST /devices/<id>/poll` then `GET /devices/<id>/latest`
- chart: `GET /devices/<id>/graph.png?start=&end=` returns 200 and ~1400 px wide; a height in
  the thousands means an annotation escaped the axes
- failure path: `journalctl --user -u tuya-monitor-collect -n 50`

## Pitfalls

- `tuya.m.my.group.device.list` needs `gid` as a **query** parameter; in `postData` it returns
  `USER_GROUP_ID_IS_BLANK`.
- The QR login result arrives nested: the `sid` is at `result["sid"]`, not `out["sid"]`.
- The `sid` expires in hours. Local keys are the durable artefact — never make polling depend
  on the session.
- Password login is unreliable (`USER_PASSWD_WRONG` even with the exact node-rsa flow:
  RSA no-padding over `md5(password)`). Prefer QR.
- Protocol errors are diagnostic: `904` = it answered with unexpected framing, `914` = key or
  version mismatch. A working device returns `dps` at exactly one version.
- `local_key` rotates on every re-pair. Nothing derives it locally; re-pair and `verify`.
- Newer firmware disables UDP discovery on 6666/6667 — `tinytuya scan` finds nothing even on
  a live device. Probe TCP 6668 and the `80:64:7c` OUI instead.
- A device is disabled after 5 consecutive failures and raises `device_offline`. `verify`
  re-enables it; `last_error` keeps the reason while it is off.
- Secrets: `data/` holds `localKey` values and the live `sid`. `git add -f data/` publishes
  them. See `SECURITY.md`.
