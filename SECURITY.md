# Secrets and what is safe to publish

Nothing personal is tracked. This file exists so that stays true.

## Tracked, and not secret

- `config.example.toml` — placeholders only (`api_key = "change-me"`).
- Tuya **app** identity in `monitor/config.py` (`clientId`, `appSecret`, `secret2`, cert
  SHA-256). These belong to the Tuya Smart 7.12.6 build, not to a person; every install of
  that app carries them and they are widely published. They break only when Tuya ships a new
  build — then re-extract per the `tuya-app-api-local-key` skill.
- The device id in `docs/adr/0001-architecture.md`. Not a credential.

## Untracked, and secret

| path | contains | mode |
|---|---|---|
| `~/.config/tuya-monitor/config.toml` | the API key for this service | 600 |
| `data/registry.db` | per-device `local_key` **and** the cloud `sid` | 700 dir |
| `data/<dev_id>.db` | telemetry; no credentials | 700 dir |

`data/`, `reports/` and `.venv/` are in `.gitignore` and verified ignored.

## Hazards

- `git add -f data/` publishes every `localKey` and the live `sid`. Prefer a fresh
  `data/` if you ever want history in the repo.
- A committed screenshot or report PNG shows the device name, not
  the IP or key.
- `~/.hermes/cache/scratch/tuya/device.json` (outside the repo) is the one file holding a
  plaintext key.

## If a key leaks

Re-pair the device in the app — `localKey` rotates and the old one dies. Rotate the API key
by editing `api_key` in the config and restarting `tuya-monitor-api`.
