import time

import pytest

from monitor import config, store


def test_outside_rows_round_trip():
    now = int(time.time())
    store.add_weather([(now, 10.5, 61.0, 0.0, 3, 22.0)])
    rows = store.weather_series(now - 60, now + 60)
    assert rows and rows[0]["temp"] == 10.5 and rows[0]["aqi"] == 22.0


def test_history_prefers_the_stored_rows(monkeypatch):
    from monitor import weather as w

    now = int(time.time())
    rows = [(now - h * 3600, 10.0 + h, 60.0, 0.0, 1, 20.0) for h in range(6)]
    store.add_weather(rows)
    monkeypatch.setattr(w, "_get", lambda url: pytest.fail("should not leave the database"))
    got = w.history(config.Settings(), now - 5 * 3600, now)
    assert got and len(got["ts"]) == 6 and got["weather_code"][0] == 1


def _wipe_weather():
    with store.registry() as con:
        con.execute("DELETE FROM weather")


def test_history_backfills_a_gap(monkeypatch):
    from monitor import weather as w

    _wipe_weather()
    w._hist_cache.clear()
    now = int(time.time())
    payload = {"hourly": {"time": [time.strftime("%Y-%m-%dT%H:%M", time.gmtime(now - 7200)),
                                   time.strftime("%Y-%m-%dT%H:%M", time.gmtime(now - 3600))],
                          "temperature_2m": [9.0, 9.5], "relative_humidity_2m": [70.0, 68.0],
                          "precipitation": [0.2, 0.0], "weather_code": [61, 3]}}
    monkeypatch.setattr(w, "_get", lambda url: payload if "air-quality" not in url else {})
    w.history(config.Settings(), now - 7200, now)
    rows = store.weather_series(now - 7300, now)
    assert any(r["code"] == 61 for r in rows), "the fetched hours have to be stored"


def test_weather_prune_drops_old_hours():
    old = int(time.time()) - 40 * 86400
    store.add_weather([(old, 5.0, 50.0, 0.0, 0, 10.0)])
    assert store.prune_weather(30) == 1
