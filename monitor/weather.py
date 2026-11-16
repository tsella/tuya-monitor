"""Outside weather, used to explain and damp weather-driven indoor readings.

A rainy day moves indoor humidity up and temperature down. Left alone, the baseline rule reads
both as anomalies. One cached lookup per TTL is enough to tell "weather" from "something wrong
in the room".
"""
from __future__ import annotations

import datetime
import json
import logging
import time
import urllib.parse
import urllib.request

from . import config, store

log = logging.getLogger("tuya-monitor.weather")

_cache: dict[str, tuple[float, dict | None]] = {}
_last_good: dict[str, tuple[float, dict]] = {}

TTL = 900          # seconds; a forecast does not move faster than this
FAIL_TTL = 120     # a failed lookup is retried sooner than a good one is refreshed
STALE_MAX = 3600   # how long the last good reading may stand in while lookups fail
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
        got = {"rh": float(cur.get("relative_humidity_2m") or 0.0),
               "temp": float(cur.get("temperature_2m") or 0.0),
               "precip": max([float(p or 0.0) for p in hourly] + [0.0]),
               "ts": time.time()}
        store.add_weather([(int(got["ts"]), got["temp"], got["rh"], got["precip"], None, None)])
        return got
    except Exception:  # noqa: BLE001 - weather is context, never critical
        return None


def outdoor(settings: config.Settings) -> dict | None:
    """Cached outside conditions, or None when disabled/unavailable."""
    if not getattr(settings, "weather_enabled", False):
        return None
    fresh = store.latest_weather(max_age=1200)
    if fresh:
        return {"rh": fresh["rh"], "temp": fresh["temp"], "precip": fresh["precip"] or 0.0,
                "ts": fresh["ts"]}

    key = f"{settings.weather_lat},{settings.weather_lon}"
    hit = _cache.get(key)
    if hit:
        age = time.time() - hit[0]
        if hit[1] is not None and age < TTL:
            return hit[1]
        if hit[1] is None and age < FAIL_TTL:
            return _stale(key)
    got = _fetch(settings)
    _cache[key] = (time.time(), got)
    if got is None:
        log.warning("weather lookup failed for %s", key)
    else:
        _last_good[key] = (time.time(), got)
    return got if got is not None else _stale(key)


def _stale(key: str) -> dict | None:
    """The last good reading, while it is still young enough to mean something."""
    good = _last_good.get(key)
    if good and time.time() - good[0] < STALE_MAX:
        return {**good[1], "stale": True}
    return None


def is_wet(ctx: dict | None) -> bool:
    return bool(ctx) and (ctx["rh"] >= WET_RH or ctx["precip"] > 0.0)


def note(ctx: dict | None) -> str:
    if not ctx:
        return ""
    bits = [f"outside {ctx['temp']:.0f}°C", f"{ctx['rh']:.0f}% RH"]
    if ctx["precip"] > 0:
        bits.append("rain")
    return " (" + ", ".join(bits) + ")"


def reset_cache() -> None:
    _cache.clear()


def _get(url: str) -> dict | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "tuya-monitor/1 (weather context)"})
        with urllib.request.urlopen(req, timeout=20) as fh:
            return json.loads(fh.read())
    except Exception:  # noqa: BLE001
        return None


def _shape(rows: list[dict]) -> dict:
    """Rows to the series shape the charts read."""
    return {"ts": [r["ts"] for r in rows],
            "temperature_2m": [r["temp"] for r in rows],
            "relative_humidity_2m": [r["rh"] for r in rows],
            "precipitation": [r["precip"] for r in rows],
            "weather_code": [r["code"] for r in rows],
            "aqi": [r["aqi"] for r in rows]}


def _store(data: dict) -> None:
    store.add_weather([(ts, tp, rh, pr, int(cd) if cd is not None else None, aq)
                       for ts, tp, rh, pr, cd, aq in zip(data["ts"], data["temperature_2m"],
                                                        data["relative_humidity_2m"],
                                                        data["precipitation"], data["weather_code"],
                                                        data["aqi"])])


def _hourly(url: str, keys: tuple[str, ...], start: int, end: int) -> dict[str, list]:
    """Hourly series clipped to the window, as parallel lists of timestamps and values."""
    out: dict[str, list] = {k: [] for k in keys}
    out["ts"] = []
    try:
        got = _get(url)
    except Exception:  # noqa: BLE001 - the chart must survive a broken lookup
        got = None
    if not got:
        return out
    hourly = got.get("hourly") or {}
    times = hourly.get("time") or []
    for i, stamp in enumerate(times):
        ts = int(datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M").replace(
            tzinfo=datetime.timezone.utc).timestamp())
        if ts < start - 3600 or ts > end + 3600:
            continue
        out["ts"].append(ts)
        for k in keys:
            series = hourly.get(k) or []
            out[k].append(series[i] if i < len(series) else None)
    return out


_hist_cache: dict[tuple, tuple[float, dict]] = {}
HIST_TTL = 600


def history(settings: config.Settings, start: int, end: int) -> dict | None:
    """Outside conditions across a window: for the graph, never for an alert.

    Returns hourly temperature, humidity, precipitation, WMO weather code and European AQI, or
    None when the window is too far back or the lookup fails.
    """
    if not getattr(settings, "weather_enabled", False):
        return None
    days = min(92, max(1, int((time.time() - start) / 86400) + 2))
    if time.time() - start > 92 * 86400:
        return None
    # hourly rows are stamped on the minute, so the window needs a little slack at its start
    have = store.weather_series(start - 1800, end)
    hours = max(1, (end - start) // 3600)
    if len(have) >= hours * 0.6:
        return _shape(have)

    key = (start // 3600, end // 3600, days)
    hit = _hist_cache.get(key)
    if hit and time.time() - hit[0] < HIST_TTL:
        _store(hit[1])          # a cache hit does not mean the database already has it
        return hit[1]
    lat, lon = settings.weather_lat, settings.weather_lon
    base = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
            f"&past_days={days}&forecast_days=1&timezone=UTC&hourly=")
    data = _hourly(base + "temperature_2m,relative_humidity_2m,precipitation,weather_code",
                   ("temperature_2m", "relative_humidity_2m", "precipitation", "weather_code"),
                   start, end)
    air = _hourly("https://air-quality-api.open-meteo.com/v1/air-quality"
                  f"?latitude={lat}&longitude={lon}&past_days={days}&forecast_days=1"
                  "&timezone=UTC&hourly=european_aqi", ("european_aqi",), start, end)
    aqi_by_ts = dict(zip(air["ts"], air["european_aqi"]))
    data["aqi"] = [aqi_by_ts.get(t) for t in data["ts"]]
    _store(data)
    if not data["ts"]:
        return None
    _hist_cache[key] = (time.time(), data)
    return data
