# State machines, invariants and reachable violations

Scope: every explicit or implicit state machine in `monitor/`, as of commit `40e4226`.
Method: enumerating writes and the fields they touch, then checking each transition against
the invariants the surrounding code assumes. Every "reachable" claim below is backed by a
regression test in `tests/test_state_machines.py` that failed before its fix.

Status legend: **OPEN** · **FIXED** (test named) · **ACCEPTED** (deliberate, documented).

## Machine map

| id | machine | owner | persisted as |
|---|---|---|---|
| M1 | device lifecycle | `store.add_device`, `set_enabled`, `record_fail`, collector | `devices.enabled/failures` |
| M2 | pair session | `POST /pair`, `GET /pair/{id}` | `pair_sessions.status` |
| M3 | collector worker | `collector.Worker`, `Supervisor.sync` | thread liveness + `devices.enabled` |
| M4 | alert | `anomaly._emit`, `notify`, `ack` | `alerts.notified_at/acked_at` |
| M5 | sample row | `insert_samples` | `samples(ts,dp)` primary key |

## M1 — device lifecycle

States: `NEW` (row absent) → `PAIRED` (`enabled=0`, key stored) → `LIVE` (`enabled=1`) →
`FAILING` (`failures>0`, still `enabled=1`) → `DISABLED` (`enabled=0`, error kept).

Invariants
- I1.1 A row is only `LIVE` after a successful `realtime.read` of that key (verify).
- I1.2 `local_key` is written only from a cloud response or an explicit import.
- I1.3 A device with `failures >= 5` is not `LIVE`.
- I1.4 Re-pairing refreshes identity fields but must not silently change `enabled`.

Transition table

| from | to | trigger | allowed |
|---|---|---|---|
| NEW | PAIRED | `POST /pair` import | yes |
| PAIRED | LIVE | `POST /devices/{id}/verify` (read OK) | yes |
| LIVE | FAILING | `record_fail` | yes |
| FAILING | LIVE | `record_ok` | yes |
| FAILING | DISABLED | `record_fail` ×5 | yes |
| DISABLED | LIVE | `verify` | yes |
| any | FAILING | `record_fail` on disabled row | yes, but see V1.4 |
| LIVE | DISABLED | **re-pair import (upsert)** | **no — V1.1** |
| PAIRED (with edits) | PAIRED | re-pair restores `version=0`, `dp_meta` defaults | **no — V1.2** |

Reachable violations
- **V1.1 (High)** — `store.add_device` upserts with `enabled=excluded.enabled` (store.py:134-138)
  and every import path passes the default `enabled=False`. Pairing a second device — or
  re-pairing after a key rotation — stops collection on every device already in the
  response until each is re-verified. Evidence: `test_repairing_does_not_disable_a_live_device`.
- **V1.2 (Med)** — same upsert overwrites `version` with 0.0 and `dp_meta` with the built-in
  default, discarding a working protocol version and any tuned metadata.
- **V1.3 (Low)** — `set_enabled(dev, False)` nulls `last_error` (store.py:168-171), so a
  manual disable erases why it was disabled.
- **V1.4 (ACCEPTED)** — a poll already in flight when a device is disabled still writes its
  sample and calls `record_ok`. One row after the cut-off is harmless.

## M2 — pair session

States: `pending` → `approved` | `expired` | `failed`. Invariants
- I2.1 exactly one state transition per session; terminal states are terminal
- I2.2 `pending` older than `expires` becomes `expired`
- I2.3 a session never imports more than once
- I2.4 no network call runs while a write transaction is open

Violations
- **V2.1 (Med)** — the old handler held a registry **write** transaction open across two
  cloud calls (`qr_poll`, `list_devices`) and then called `store.add_device`, which opens a
  *second* registry connection while the first is still in flight. The snapshot it later
  writes against predates the import, and a concurrent writer (collector, a second poll)
  competes for the single write lock for as long as the network takes.
- **V2.2 (High)** — because the import loop and the `approved` write shared that one
  transaction, an exception partway through the loop rolled back the status but not the
  devices already inserted on *other* connections. The session stayed `pending`, so the next
  poll re-imported everything — which, together with V1.1, disabled the devices it had just
  created. Evidence: `test_approved_session_does_not_reimport`.
- **V2.3 (Med)** — expiry was checked only in `pending`; `approved` rows lived forever and
  `pair_sessions` was never pruned.
- **V2.4 (Med)** — only `CloudError` was caught around `list_devices`, so a timeout or a
  JSON error escaped as a 500 and left the session `pending` — the same re-import path as
  V2.2. Evidence: `test_pair_survives_a_non_cloud_exception`.
- **V2.5 (ACCEPTED after fix)** — two concurrent polls can still both reach the import loop
  before either transitions. That is harmless because `add_device` is now idempotent and
  never touches `enabled`; the second request reports the winner's status.

## M3 — collector worker

States: `absent` → `spawned` → `running` → `exiting` → `absent`. Invariants
- I3.1 one worker per enabled device
- I3.2 a worker never outlives `enabled=0`
- I3.3 `Supervisor.workers` entries are removed for dead threads

Violations
- **V3.1 (Med)** — `Worker.run` returns on `not enabled` without touching the supervisor map;
  the entry lingers until the next `sync()` (collector.py:35-37). `sync` is the only reaper,
  so a stop-then-start inside 5 s can be masked, and `stop()` on the stale entry is a no-op.
  Accepted as self-healing, but documented because it makes `workers` a lagging view.
- **V3.2 (Low)** — `sync()` holds `self.lock` while calling `worker.start()`; starting a
  thread that immediately calls `store.get_device` is safe, but any future work added to
  `start()` would serialise the supervisor.

## M4 — alert

States: `open` → `notified` → `acked`. Invariants
- I4.1 one row per `(device, dp, rule)` per cooldown window
- I4.2 `notified_at` is set only by the notifier
- I4.3 an acked alert is not re-notified

Violations
- **V4.1 (Med)** — `unread_alerts` filters `notified_at IS NULL OR acked_at IS NULL`
  (store.py:264). A notified-but-unacked row is therefore returned on every call; a
  consumer that prints all rows rather than filtering `notified_at` repeats itself forever.
  `cli.notify` happens to filter, the API does not. Evidence:
  `test_alerts_endpoint_keeps_acked_rows_out_of_the_open_set` (after fix: acked rows excluded).
- **V4.2 (Low)** — `ack` on an already-acked row returns `False` and the API turns that into
  `{"acked": false}` with 200, which is indistinguishable from "unknown id".

## M5 — sample row

Invariant I5.1 `(ts, dp)` unique, so a retried poll cannot double-count. Holds by primary key.
Violation: none found. Boundary case: a poll that crosses a second boundary writes two rows,
which is correct but makes `ts` non-monotonic relative to insertion order — accepted.

## Cross-cutting

- **VX.1 (Med)** — `GET /series` has no range cap while `/graph.png` caps at 90 days; a
  10-year raw range loads every row into memory. Evidence: `test_series_is_range_capped`.
- **VX.2 (Low)** — the prune tick is `int(time.time()) % 600 < poll_interval`
  (collector.py:88): with a 30 s interval it fires twice per 10 minutes, 30 s apart, and
  never if the interval exceeds 600. Evidence: `test_prune_scheduler_runs_once_per_period`.
- **VX.3 (Low)** — `report_retention_days = 0` now means "keep reports forever", matching
  `retention_days = 0`; documented in `config.example.toml`.

## Issues (fix order = impact)

- **IS-1 (V1.1, V1.2, V2.2)** — pairing/import destroys live state. Fix: `add_device`
  preserves `enabled`, `version` and `dp_meta` on conflict unless the caller asks otherwise;
  the pairing import passes `enabled=None` meaning "leave as is"; new rows stay disabled.
- **IS-2 (V2.1, V2.3, V2.4)** — pair handler transaction and lifecycle. Fix: read/decide in
  short transactions, network outside them, one atomic `approved` write guarded by
  `WHERE status='pending'`, expiry enforced for every non-terminal state, sessions older than
  24 h deleted.
- **IS-3 (V4.1, V4.2)** — alert set semantics. Fix: `open_alerts()` returns unacked rows for
  the API, `fresh_alerts()` returns un-notified rows for the notifier; `ack` on an acked row
  returns `True` (idempotent) and 404 only for unknown ids.
- **IS-4 (VX.1, VX.2)** — read cost and prune cadence. Fix: cap `series` at 90 days unless
  `bucket > 0`; prune on an elapsed-time stamp instead of a modulo.
- **IS-5 (V1.3)** — keep `last_error` when disabling.

## Status

| id | state | test |
|---|---|---|
| IS-1 | FIXED | `test_repairing_does_not_disable_a_live_device`, `test_import_keeps_version_and_metadata` |
| IS-2 | FIXED | `test_approved_session_does_not_reimport`, `test_expired_session_is_terminal`, `test_pair_survives_a_non_cloud_exception` |
| IS-3 | FIXED | `test_alerts_endpoint_keeps_acked_rows_out_of_the_open_set`, `test_ack_is_idempotent`, `test_unknown_ack_is_404`, `test_notifier_only_sees_unnotified_rows` |
| IS-4 | FIXED | `test_series_is_range_capped`, `test_series_long_range_allowed_when_bucketed`, `test_prune_scheduler_runs_once_per_period` |
| IS-5 | FIXED | `test_disabling_keeps_the_reason` |
| V1.4, V2.5, V3.1, V3.2, M5 | ACCEPTED | — |

## Fix notes

- IS-1 `add_device(enabled=None)` is the new default: new rows land disabled, existing rows
  keep their flag, `version` and `dp_meta` only move when the caller supplies a real value
  or the category changes. Pairing imports pass `None`.
- IS-2 the handler now reads once, calls the network with no transaction open, imports
  idempotently, and finishes with `UPDATE … WHERE status='pending'` so exactly one request
  can approve. Expiry applies to every non-terminal state, and sessions older than 24 h are
  deleted on the next `POST /pair`. Non-cloud exceptions land the session in `failed`.
- IS-3 `open_alerts()` (unacked, for the API) and `fresh_alerts()` (unnotified, for the
  notifier) replace the ambiguous `unread_alerts()`. `ack` is idempotent; unknown ids 404.
- IS-4 `series` refuses raw ranges over 90 days and points at `bucket`; pruning is gated on
  elapsed time (`PRUNE_PERIOD`), not on wall-clock modulo.
- IS-5 disabling preserves `last_error`; only enabling clears it.
