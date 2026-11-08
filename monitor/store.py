"""Storage: registry DB (identity, keys, alerts) + one telemetry DB per device."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import config

SCHEMA_VERSION = 1

REGISTRY_SQL = """
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS devices (
    dev_id       TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    category     TEXT NOT NULL DEFAULT '',
    product_id   TEXT NOT NULL DEFAULT '',
    ip           TEXT NOT NULL DEFAULT '',
    local_key    TEXT NOT NULL DEFAULT '',
    version      REAL NOT NULL DEFAULT 0,
    dp_meta      TEXT NOT NULL DEFAULT '{}',
    enabled      INTEGER NOT NULL DEFAULT 0,
    paired_at    INTEGER NOT NULL,
    last_seen    INTEGER,
    last_error   TEXT,
    failures     INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS pair_sessions (
    id         TEXT PRIMARY KEY,
    token      TEXT NOT NULL,
    created    INTEGER NOT NULL,
    expires    INTEGER NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',
    detail     TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS alerts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    dev_id     TEXT NOT NULL,
    dp         TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    rule       TEXT NOT NULL,
    severity   TEXT NOT NULL,
    value      REAL,
    baseline   REAL,
    message    TEXT NOT NULL,
    acked_at   INTEGER,
    notified_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_open ON alerts(notified_at) WHERE notified_at IS NULL;
"""

DEVICE_SQL = """
CREATE TABLE IF NOT EXISTS samples (
    ts  INTEGER NOT NULL,
    dp  TEXT NOT NULL,
    num REAL,
    txt TEXT,
    PRIMARY KEY (ts, dp)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_samples_dp_ts ON samples(dp, ts);
"""


def _connect(path: Path, ro: bool = False, schema: str | None = None) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if ro and not path.exists():  # mode=ro cannot create the file
        _connect(path, schema=schema).close()
    uri = f"file:{path}?mode=ro" if ro else f"file:{path}"
    con = sqlite3.connect(uri, uri=True, timeout=15.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=15000")
    if not ro:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        if schema:
            con.executescript(schema)
    con.execute("PRAGMA foreign_keys=ON")
    return con


@contextmanager
def registry(write: bool = True) -> Iterator[sqlite3.Connection]:
    con = _connect(config.DATA / "registry.db", ro=not write, schema=REGISTRY_SQL)
    try:
        if write:
            con.execute("INSERT OR IGNORE INTO meta(k, v) VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
        yield con
        if write:
            con.commit()
    except Exception:
        if write:
            con.rollback()
        raise
    finally:
        con.close()


def _device_path(dev_id: str) -> Path:
    safe = "".join(c for c in dev_id if c.isalnum() or c in "-_")[:40] or "device"
    return config.DATA / f"{safe}.db"


@contextmanager
def telemetry(dev_id: str, write: bool = True) -> Iterator[sqlite3.Connection]:
    path = _device_path(dev_id)
    con = _connect(path, ro=not write, schema=DEVICE_SQL)
    try:
        yield con
        if write:
            con.commit()
    except Exception:
        if write:
            con.rollback()
        raise
    finally:
        con.close()


# ---------------------------------------------------------------- devices

def add_device(dev_id: str, name: str, ip: str, local_key: str, *, category: str = "",
               product_id: str = "", version: float = 0.0,
               enabled: bool | None = None) -> dict:
    """Insert or refresh a device row.

    `enabled=None` (the default) means: a new row lands disabled, an existing row keeps
    whatever flag it had. That is what lets a re-pair refresh identity without stopping
    collection. Pass a bool to force the flag.
    """
    dp_meta = json.dumps(config.DP_META.get(category, {}))
    with registry() as con:
        con.execute(
            """INSERT INTO devices (dev_id, name, category, product_id, ip, local_key,
                                    version, dp_meta, enabled, paired_at)
               VALUES (?,?,?,?,?,?,?,?,COALESCE(?,0),?)
               ON CONFLICT(dev_id) DO UPDATE SET name=excluded.name, ip=excluded.ip,
                   local_key=excluded.local_key, category=excluded.category,
                   product_id=excluded.product_id,
                   version=CASE WHEN excluded.version > 0 THEN excluded.version
                                ELSE devices.version END,
                   dp_meta=CASE WHEN devices.dp_meta = '{}'
                                     OR devices.category != excluded.category
                                THEN excluded.dp_meta ELSE devices.dp_meta END""",
            (dev_id, name, category, product_id, ip, local_key, version,
             dp_meta, None if enabled is None else int(enabled), int(time.time())))
        if enabled is not None:
            con.execute("UPDATE devices SET enabled=? WHERE dev_id=?", (int(enabled), dev_id))
        if enabled:
            con.execute("UPDATE devices SET failures=0, last_error=NULL WHERE dev_id=?", (dev_id,))
    with telemetry(dev_id) as tcon:  # create the file so the UI has something to read
        tcon.execute("INSERT OR IGNORE INTO samples(ts, dp, num, txt) SELECT 0, '__init__', 0, NULL")
    return get_device(dev_id) or {}


def _row(row: sqlite3.Row) -> dict:
    dev = dict(row)
    try:
        dev["dp_meta"] = json.loads(dev.get("dp_meta") or "{}")
    except (TypeError, ValueError):
        dev["dp_meta"] = {}
    return dev


def get_device(dev_id: str) -> dict | None:
    with registry(write=False) as con:
        row = con.execute("SELECT * FROM devices WHERE dev_id=?", (dev_id,)).fetchone()
    return _row(row) if row else None


def list_devices() -> list[dict]:
    with registry(write=False) as con:
        rows = con.execute("SELECT * FROM devices ORDER BY name").fetchall()
    return [_row(r) for r in rows]


def set_enabled(dev_id: str, enabled: bool) -> None:
    """Flip the flag. Disabling keeps `last_error` as the reason; enabling clears it."""
    with registry() as con:
        con.execute(
            "UPDATE devices SET enabled=?, failures=0, "
            "last_error=CASE WHEN ? THEN last_error ELSE NULL END WHERE dev_id=?",
            (int(enabled), int(not enabled), dev_id))


def record_ok(dev_id: str) -> None:
    with registry() as con:
        con.execute("UPDATE devices SET last_seen=?, failures=0, last_error=NULL WHERE dev_id=?",
                    (int(time.time()), dev_id))


def record_fail(dev_id: str, err: str) -> int:
    with registry() as con:
        con.execute("UPDATE devices SET failures=failures+1, last_error=? WHERE dev_id=?",
                    (err[:300], dev_id))
        n = con.execute("SELECT failures FROM devices WHERE dev_id=?", (dev_id,)).fetchone()
        return int(n[0]) if n else 0


# ---------------------------------------------------------------- samples

def insert_samples(dev_id: str, rows: Iterable[tuple[int, str, float | None, str | None]]) -> int:
    data = list(rows)
    if not data:
        return 0
    with telemetry(dev_id) as con:
        con.executemany("INSERT OR IGNORE INTO samples(ts, dp, num, txt) VALUES (?,?,?,?)", data)
    return len(data)


def series(dev_id: str, start: int, end: int, dp: str | None = None,
           bucket: int = 0) -> list[dict]:
    """Raw or bucketed rows. bucket>0 aggregates into fixed windows of that many seconds."""
    with telemetry(dev_id, write=False) as con:
        if bucket > 0:
            sql = ("SELECT (ts/?)*? AS t, dp, MIN(num) AS lo, MAX(num) AS hi, "
                   "AVG(num) AS avg, COUNT(*) AS n FROM samples "
                   "WHERE ts BETWEEN ? AND ? AND num IS NOT NULL")
            args: list[Any] = [bucket, bucket, start, end]
            if dp:
                sql += " AND dp=?"; args.append(dp)
            sql += " GROUP BY t, dp ORDER BY t"
            return [dict(r) for r in con.execute(sql, args)]
        sql = "SELECT ts, dp, num, txt FROM samples WHERE ts BETWEEN ? AND ?"
        args = [start, end]
        if dp:
            sql += " AND dp=?"; args.append(dp)
        sql += " ORDER BY ts"
        return [dict(r) for r in con.execute(sql, args)]


def latest(dev_id: str) -> dict[str, Any]:
    with telemetry(dev_id, write=False) as con:
        rows = con.execute(
            """SELECT s.dp, s.num, s.txt, s.ts FROM samples s
               JOIN (SELECT dp, MAX(ts) m FROM samples WHERE dp != '__init__' GROUP BY dp) x
                 ON s.dp = x.dp AND s.ts = x.m""").fetchall()
    return {r["dp"]: {"value": r["num"] if r["num"] is not None else r["txt"], "ts": r["ts"]} for r in rows}


def prune(dev_id: str, keep_days: int) -> int:
    if keep_days <= 0:  # uncapped retention
        return 0
    cutoff = int(time.time()) - keep_days * 86400
    with telemetry(dev_id) as con:
        cur = con.execute("DELETE FROM samples WHERE ts < ? AND dp != '__init__'", (cutoff,))
        n = cur.rowcount
        con.execute("PRAGMA incremental_vacuum" if False else "PRAGMA optimize")
    return n


# ---------------------------------------------------------------- alerts

def add_alert(dev_id: str, dp: str, rule: str, severity: str, value: float | None,
              baseline: float | None, message: str) -> int:
    with registry() as con:
        cur = con.execute(
            "INSERT INTO alerts(dev_id, dp, ts, rule, severity, value, baseline, message) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (dev_id, dp, int(time.time()), rule, severity, value, baseline, message))
        return int(cur.lastrowid or 0)


def recent_alert(dev_id: str, dp: str, rule: str, within: int) -> dict | None:
    with registry(write=False) as con:
        row = con.execute(
            "SELECT * FROM alerts WHERE dev_id=? AND dp=? AND rule=? AND ts > ? "
            "ORDER BY ts DESC LIMIT 1", (dev_id, dp, rule, int(time.time()) - within)).fetchone()
    return dict(row) if row else None


def open_alerts(limit: int = 100) -> list[dict]:
    """Unacked alerts, newest first — what an operator still has to deal with."""
    with registry(write=False) as con:
        rows = con.execute(
            """SELECT a.*, d.name FROM alerts a LEFT JOIN devices d ON d.dev_id = a.dev_id
               WHERE a.acked_at IS NULL ORDER BY a.ts DESC LIMIT ?""", (limit,)).fetchall()
        return [dict(r) for r in rows]


def fresh_alerts(limit: int = 100) -> list[dict]:
    """Unnotified, unacked alerts — what the notifier has not sent yet."""
    with registry(write=False) as con:
        rows = con.execute(
            """SELECT a.*, d.name FROM alerts a LEFT JOIN devices d ON d.dev_id = a.dev_id
               WHERE a.notified_at IS NULL AND a.acked_at IS NULL
               ORDER BY a.ts DESC LIMIT ?""", (limit,)).fetchall()
        return [dict(r) for r in rows]


def mark_notified(ids: list[int]) -> None:
    if not ids:
        return
    with registry() as con:
        qs = ",".join("?" * len(ids))
        con.execute(f"UPDATE alerts SET notified_at=? WHERE id IN ({qs})",
                    [int(time.time()), *ids])


def ack(alert_id: int) -> bool:
    """Idempotent. False only when the id does not exist."""
    with registry() as con:
        row = con.execute("SELECT id FROM alerts WHERE id=?", (alert_id,)).fetchone()
        if not row:
            return False
        con.execute("UPDATE alerts SET acked_at=COALESCE(acked_at, ?) WHERE id=?",
                    (int(time.time()), alert_id))
        return True
