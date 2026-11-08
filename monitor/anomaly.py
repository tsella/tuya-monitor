"""Anomaly rules: static bounds plus a robust same-hour baseline."""
from __future__ import annotations

import statistics
import time
from typing import Any

from . import config, realtime, store

COOLDOWN = None  # filled from settings at call time


def _baseline(dev_id: str, dp: str, ts: int, days: int) -> tuple[float, float, int]:
    """Median and MAD of this hour-of-day over previous days.

    The window is at least 14 days: a device sampled once an hour yields only one point
    per day, and a 7-day window can never reach the 12-point minimum on its own.
    """
    hour = (ts % 86400) // 3600
    window = max(days, 14)
    lo = ts - window * 86400 - 1800
    hi = ts - 1800
    with store.telemetry(dev_id, write=False) as con:
        rows = con.execute(
            "SELECT num FROM samples WHERE dp=? AND num IS NOT NULL AND ts BETWEEN ? AND ? "
            "AND ((ts % 86400) / 3600) = ?", (dp, lo, hi, hour)).fetchall()
    vals = [r[0] for r in rows if r[0] is not None]
    if len(vals) < 12:
        return (0.0, 0.0, len(vals))
    med = statistics.median(vals)
    mad = statistics.median([abs(v - med) for v in vals]) or 0.0
    return (med, mad, len(vals))


def _emit(dev: dict, dp: str, rule: str, severity: str, value: float, baseline: float | None,
          text: str, cooldown_s: int) -> dict | None:
    if store.recent_alert(dev["dev_id"], dp, rule, cooldown_s):
        return None
    store.add_alert(dev["dev_id"], dp, rule, severity, value, baseline, text)
    return {"dev_id": dev["dev_id"], "dp": dp, "rule": rule, "severity": severity,
            "value": value, "message": text}


def _fmt(value: float, kind: str, unit: str, settings: config.Settings) -> str:
    if kind == "temp":
        if settings.temp_unit == "f":
            return f"{config.c_to_f(value):.1f}°F"
        if settings.temp_unit == "both":
            return f"{value:.1f}°C / {config.c_to_f(value):.1f}°F"
    return f"{value:.1f}{unit}" if kind != "co2" else f"{value:.0f}{unit}"


def evaluate(dev: dict, dps: dict[str, Any], settings: config.Settings,
             ts: int | None = None) -> list[dict]:
    if not settings.anomaly_enabled:
        return []
    ts = ts or int(time.time())
    cooldown = settings.cooldown_min * 60
    out: list[dict] = []
    for dp, raw in dps.items():
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        kind = realtime.meta_for(dev.get("category", ""), dp).get("kind")
        if not kind:
            continue
        th = settings.thresholds.get(kind)
        if not th:
            continue
        label = realtime.meta_for(dev["category"], dp).get("name", dp)
        unit = realtime.meta_for(dev["category"], dp).get("unit", "")
        prefix = f"{dev['name']} · {label}"
        shown = _fmt(value, kind, unit, settings)

        if "warn" in th and value >= th["warn"]:
            sev = "alert" if value >= th["alert"] else "warn"
            got = _emit(dev, dp, f"static_high_{sev}", sev, value, th["warn"],
                        f"{prefix} at {shown} (limit {_fmt(th['warn'], kind, unit, settings)})", cooldown)
            if got:
                out.append(got)
                continue
        if "warn_high" in th and (value >= th["warn_high"] or value <= th["warn_low"]):
            sev = "alert" if (value >= th["alert_high"] or value <= th["alert_low"]) else "warn"
            edge = th["warn_high"] if value >= th["warn_high"] else th["warn_low"]
            band = f"{_fmt(th['warn_low'], kind, unit, settings)}–{_fmt(th['warn_high'], kind, unit, settings)}"
            got = _emit(dev, dp, f"static_range_{sev}", sev, value, edge,
                        f"{prefix} at {shown}, outside {band}", cooldown)
            if got:
                out.append(got)

        med, mad, n = _baseline(dev["dev_id"], dp, ts, settings.baseline_days)
        if n >= 12:
            band = max(4 * mad, th.get("floor", 0.0))
            if abs(value - med) > band:
                sev = "warn" if abs(value - med) < band * 2 else "alert"
                got = _emit(dev, dp, "baseline", sev, value, med,
                            f"{prefix} at {shown}, unusual for this hour "
                            f"(median {_fmt(med, kind, unit, settings)}, {n} samples)", cooldown)
                if got:
                    out.append(got)
    return out
