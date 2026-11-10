"""Anomaly rules: static bounds plus a robust same-hour baseline.

Kept deliberately dull, because a noisy monitor gets ignored. Three brakes sit in front of
every alert: the value must persist for `confirm_samples` consecutive polls, the rule must be
past its cooldown, and a baseline deviation must clear a wide MAD band. Baseline warnings
cool down hardest — "unusual for this hour" is the rule most likely to cry wolf.
"""
from __future__ import annotations

import statistics
import time
from typing import Any

from . import config, realtime, store

CONFIRM = "confirm"

# (dev_id, dp, rule) -> consecutive polls that matched
_streaks: dict[tuple[str, str, str], int] = {}

ALL_RULES = ("static_high_warn", "static_high_alert", "static_range_warn",
             "static_range_alert", "baseline")

# multiplier on settings.cooldown_min, by rule prefix
_COOLDOWN_FACTOR = {"static_high_alert": 1, "static_range_alert": 1,
                    "static_high_warn": 2, "static_range_warn": 2,
                    "baseline": 3}

BASELINE_MAD = 5.0      # band = max(5 * MAD, floor)
BASELINE_MIN_POINTS = 12  # sparse devices yield one point per day; 14-day window caps it


def reset_streaks(dev_id: str | None = None) -> None:
    if dev_id is None:
        _streaks.clear()
        return
    for key in [k for k in _streaks if k[0] == dev_id]:
        _streaks.pop(key, None)


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
          text: str, settings: config.Settings) -> dict | None:
    key = (dev["dev_id"], dp, rule)
    streak = _streaks.get(key, 0) + 1
    _streaks[key] = streak
    if streak < max(settings.confirm_samples, 1):
        return None
    cooldown = settings.cooldown_min * 60 * _COOLDOWN_FACTOR.get(rule, 1)
    if store.recent_alert(dev["dev_id"], dp, rule, cooldown):
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
        matched: set[str] = set()

        if "warn" in th and value >= th["warn"]:
            sev = "alert" if value >= th["alert"] else "warn"
            rule = f"static_high_{sev}"
            matched.add(rule)
            got = _emit(dev, dp, rule, sev, value, th["warn"],
                        f"{prefix} at {shown} (limit {_fmt(th['warn'], kind, unit, settings)})",
                        settings)
            if got:
                out.append(got)
        elif "warn_high" in th and (value >= th["warn_high"] or value <= th["warn_low"]):
            sev = "alert" if (value >= th["alert_high"] or value <= th["alert_low"]) else "warn"
            rule = f"static_range_{sev}"
            matched.add(rule)
            edge = th["warn_high"] if value >= th["warn_high"] else th["warn_low"]
            band = f"{_fmt(th['warn_low'], kind, unit, settings)}–{_fmt(th['warn_high'], kind, unit, settings)}"
            got = _emit(dev, dp, rule, sev, value, edge,
                        f"{prefix} at {shown}, outside {band}", settings)
            if got:
                out.append(got)

        med, mad, n = _baseline(dev["dev_id"], dp, ts, settings.baseline_days)
        if n >= BASELINE_MIN_POINTS:
            band = max(BASELINE_MAD * mad, th.get("floor", 0.0))
            if abs(value - med) > band:
                matched.add("baseline")
                sev = "alert" if abs(value - med) > band * 2.5 else "warn"
                got = _emit(dev, dp, "baseline", sev, value, med,
                            f"{prefix} at {shown}, unusual for this hour "
                            f"(median {_fmt(med, kind, unit, settings)}, {n} samples)", settings)
                if got:
                    out.append(got)

        for rule in set(ALL_RULES) - matched:  # condition cleared: drop the streak
            _streaks.pop((dev["dev_id"], dp, rule), None)
    return out
