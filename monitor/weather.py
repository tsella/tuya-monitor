"""Outside weather, used to explain and damp weather-driven indoor readings.

A rainy day moves indoor humidity up and temperature down. Left alone, the baseline rule reads
both as anomalies. One cached lookup per TTL is enough to tell "weather" from "something wrong
in the room".
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

from . import config

_cache: dict[str, tuple[float, dict | None]] = {}

TTL = 900          # seconds; a forecast does not move faster than this
WET_RH = 65.0      # outdoor RH at which indoor humidity rise is expected


def _fetch(settings: config.Settings) -> dict | None:
    params = urllib.parse.urlencode({
        "latitude": settings.weather_lat, "longitude": settings.weather_lon,
        "current": "relative_humidity_2m,temperature_2m,precipitation",
        "hourly": "precipitation", "forecast_hours": 3,
        "timezone": "auto"})
    url = f"https://api.open-meteo.com/v1/forecast?{params}"
    try:
        with urllib.request.urlopen(url, timeout=12) as fh:
            data = json.loads(fh.read())
        cur = data.get("current") or {}
        hourly = (data.get("hourly") or {}).get("precipitation") or []
        return {"rh": float(cur.get("relative_humidity_2m") or 0.0),
                "temp": float(cur.get("temperature_2m") or 0.0),
                "precip": max([float(p or 0.0) for p in hourly] + [0.0]),
                "ts": time.time()}
    except Exception:  # noqa: BLE001 - weather is context, never critical
        return None


def outdoor(settings: config.Settings) -> dict | None:
    """Cached outside conditions, or None when disabled/unavailable."""
    if not getattr(settings, "weather_enabled", False):
        return None
    key = f"{settings.weather_lat},{settings.weather_lon}"
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < TTL:
        return hit[1]
    got = _fetch(settings)
    _cache[key] = (time.time(), got)
    return got


def is_wet(ctx: dict | None) -> bool:
    return bool(ctx) and (ctx["rh"] >= WET_RH or ctx["precip"] > 0.0)


def note(ctx: dict | None) -> str:
    if not ctx:
        return ""
    bits = [f"outside {ctx['rh']:.0f}% RH"]
    if ctx["precip"] > 0:
        bits.append("rain")
    return " (" + ", ".join(bits) + ")"


def reset_cache() -> None:
    _cache.clear()
