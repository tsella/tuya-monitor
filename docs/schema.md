# Schema

Two kinds of database, on purpose.

## `data/registry.db` — identity and events

| table | columns | notes |
|---|---|---|
| `devices` | `dev_id` PK, `name`, `category`, `product_id`, `ip`, `local_key`, `version`, `dp_meta`, `enabled`, `paired_at`, `last_seen`, `last_error`, `failures` | `local_key` is secret; `dp_meta` is JSON dp→{name,unit,kind} |
| `pair_sessions` | `id` PK, `token`, `created`, `expires`, `status`, `detail` | status: pending → approved / expired / failed |
| `alerts` | `id`, `dev_id`, `dp`, `ts`, `rule`, `severity`, `value`, `baseline`, `message`, `acked_at`, `notified_at` | append-only |

## `data/<dev_id>.db` — telemetry, one file per device

```sql
CREATE TABLE samples (
    ts  INTEGER NOT NULL,   -- epoch seconds, UTC
    dp  TEXT NOT NULL,      -- datapoint id as the device reports it
    num REAL,               -- numeric value when the dp is numeric
    txt TEXT,               -- original text for enums like 101=('c'|'f')
    PRIMARY KEY (ts, dp)
) WITHOUT ROWID;
```

- `INSERT OR IGNORE` on `(ts, dp)`: a duplicate poll cannot double-count.
- `num` and `txt` are separate so SQL can aggregate without casting.
- A sentinel row `(0, '__init__')` exists so a freshly paired device has a readable file.

## Invariants

- `local_key` and the whole `data/` tree are 600/700.
- Samples never reference the registry; deleting `registry.db` loses pairing, not history.
- `latest()` joins on `MAX(ts)` per dp and is the only hot read path.
