import datetime
import time

import pytest

from monitor import anomaly, config, store


@pytest.fixture(autouse=True)
def clean():
    for f in config.DATA.glob("*.db*"):
        f.unlink(missing_ok=True)


@pytest.fixture
def settings():
    s = config.Settings()
    s.cooldown_min = 30
    s.baseline_days = 7
    s.confirm_samples = 1  # rule-level tests; debounce has its own tests
    anomaly.reset_streaks()
    return s


def make_dev():
    return store.add_device("dev1", "Sensor", "192.0.2.5", "k" * 16, category="co2bj")


def test_high_co2_warns_then_alerts(settings):
    dev = make_dev()
    assert anomaly.evaluate(dev, {"2": 1300}, settings)[0]["rule"] == "static_high_warn"
    dev = store.get_device("dev1")
    # inside the cooldown window the same rule is suppressed
    assert anomaly.evaluate(dev, {"2": 1400}, settings) == []
    assert store.recent_alert("dev1", "2", "static_high_warn", 60) is not None


def test_alert_severity_at_second_threshold(settings):
    dev = make_dev()
    # 1900 also crosses warn, but the emit lands on the alert rule first
    got = anomaly.evaluate(dev, {"2": 1900}, settings)
    assert got and got[0]["severity"] == "alert"


def test_temp_range_rule(settings):
    dev = make_dev()
    got = anomaly.evaluate(dev, {"18": 40.0}, settings)
    assert got and got[0]["rule"].startswith("static_range")


def test_no_alert_inside_bounds(settings):
    dev = make_dev()
    assert anomaly.evaluate(dev, {"2": 600}, settings) == []


def test_unknown_dp_is_ignored(settings):
    dev = make_dev()
    assert anomaly.evaluate(dev, {"77": 9999}, settings) == []


def test_baseline_needs_twelve_points(settings):
    dev = make_dev()
    now = int(time.time())
    day = 86400
    # 5 days of a steady 500 at the same hour → not enough for a baseline
    rows = [(now - d * day, "2", 500.0, None) for d in range(1, 6)]
    store.insert_samples("dev1", rows)
    got = anomaly.evaluate(dev, {"2": 520}, settings, ts=now)
    assert [g["rule"] for g in got] == []


def test_baseline_fires_on_real_deviation(settings):
    dev = make_dev()
    now = int(time.time())
    day = 86400
    rows = [(now - d * day, "2", 500.0 + (d % 2), None) for d in range(1, 26)]
    store.insert_samples("dev1", rows)
    got = anomaly.evaluate(dev, {"2": 700}, settings, ts=now)
    rules = [g["rule"] for g in got]
    assert "baseline" in rules


def test_disabled_switch(settings):
    dev = make_dev()
    settings.anomaly_enabled = False
    assert anomaly.evaluate(dev, {"2": 5000}, settings) == []


def test_deviation_must_persist(settings):
    dev = make_dev()
    settings.confirm_samples = 3
    assert anomaly.evaluate(dev, {"2": 1300}, settings) == []
    assert anomaly.evaluate(dev, {"2": 1310}, settings) == []
    got = anomaly.evaluate(dev, {"2": 1320}, settings)
    assert [g["rule"] for g in got] == ["static_high_warn"]


def test_streak_resets_when_it_clears(settings):
    dev = make_dev()
    settings.confirm_samples = 3
    for v in (1300, 1310, 600, 1300, 1300):
        assert anomaly.evaluate(dev, {"2": v}, settings) == []
    got = anomaly.evaluate(dev, {"2": 1300}, settings)
    assert [g["rule"] for g in got] == ["static_high_warn"]


def test_dry_room_is_inside_the_band(settings):
    dev = make_dev()
    # 28% RH used to sit below the 30% warn edge; the band now starts at 25%
    assert anomaly.evaluate(dev, {"19": 28}, settings) == []


def test_baseline_cools_down_longest():
    assert (anomaly._COOLDOWN_FACTOR["baseline"]
            > anomaly._COOLDOWN_FACTOR["static_range_warn"]
            > anomaly._COOLDOWN_FACTOR["static_range_alert"])


def _baseline_rows(now, dp, median):
    day = 86400
    return [(now - d * day, dp, median + (d % 2), None) for d in range(1, 26)]


def test_rain_widens_the_humidity_band(settings, monkeypatch):
    dev = make_dev()
    now = int(time.time())
    store.insert_samples("dev1", _baseline_rows(now, "19", 30.0))
    monkeypatch.setattr(anomaly.weather, "outdoor",
                        lambda s: {"rh": 88.0, "temp": 12.0, "precip": 1.2, "ts": now})
    # 45 % is 15 points over the dry-hour median: suppressed while it rains outside
    assert [g for g in anomaly.evaluate(dev, {"19": 45}, settings, ts=now) if g["rule"] == "baseline"] == []


def test_dry_day_keeps_the_humidity_baseline(settings, monkeypatch):
    dev = make_dev()
    now = int(time.time())
    store.insert_samples("dev1", _baseline_rows(now, "19", 30.0))
    monkeypatch.setattr(anomaly.weather, "outdoor",
                        lambda s: {"rh": 40.0, "temp": 22.0, "precip": 0.0, "ts": now})
    got = [g for g in anomaly.evaluate(dev, {"19": 45}, settings, ts=now) if g["rule"] == "baseline"]
    assert got and "22°C, 40% RH" in got[0]["message"]


def test_wetness_predicate():
    assert anomaly.weather.is_wet({"rh": 90.0, "precip": 0.0, "ts": 0}) is True
    assert anomaly.weather.is_wet({"rh": 90.0, "precip": 0.5, "ts": 0}) is True
    assert anomaly.weather.is_wet({"rh": 40.0, "precip": 0.0, "ts": 0}) is False
    assert anomaly.weather.is_wet(None) is False


def test_cold_outside_explains_a_cold_room(settings, monkeypatch):
    dev = make_dev()
    now = int(time.time())
    store.insert_samples("dev1", _baseline_rows(now, "18", 24.0))
    monkeypatch.setattr(anomaly.weather, "outdoor",
                        lambda s: {"rh": 60.0, "temp": 9.0, "precip": 0.0, "ts": now})
    got = [g for g in anomaly.evaluate(dev, {"18": 20.0}, settings, ts=now) if g["rule"] == "baseline"]
    assert got == []


def test_mild_outside_keeps_the_temperature_baseline(settings, monkeypatch):
    dev = make_dev()
    now = int(time.time())
    store.insert_samples("dev1", _baseline_rows(now, "18", 24.0))
    monkeypatch.setattr(anomaly.weather, "outdoor",
                        lambda s: {"rh": 55.0, "temp": 22.0, "precip": 0.0, "ts": now})
    got = [g for g in anomaly.evaluate(dev, {"18": 20.0}, settings, ts=now) if g["rule"] == "baseline"]
    assert got and "outside 22°C" in got[0]["message"]


def test_a_failed_lookup_falls_back_to_the_last_good_reading(monkeypatch):
    from monitor import weather as w

    s = config.Settings()
    w.reset_cache()
    w._last_good["40.7453,-73.9782"] = (time.time(), {"rh": 71.0, "temp": 8.0, "precip": 0.0, "ts": 0})
    monkeypatch.setattr(w, "_fetch", lambda settings: None)
    got = w.outdoor(s)
    assert got and got["temp"] == 8.0 and got["stale"] is True


def test_a_stale_reading_expires(monkeypatch):
    from monitor import weather as w

    s = config.Settings()
    w.reset_cache()
    w._last_good["40.7453,-73.9782"] = (time.time() - w.STALE_MAX - 5,
                                        {"rh": 71.0, "temp": 8.0, "precip": 0.0, "ts": 0})
    monkeypatch.setattr(w, "_fetch", lambda settings: None)
    assert w.outdoor(s) is None


def _ctx(temp, rh, now=None):
    return {"rh": rh, "temp": temp, "precip": 0.0, "ts": now or int(time.time())}


def test_warm_room_on_an_ordinary_day_is_called_out(settings, monkeypatch):
    dev = make_dev()
    now = int(time.time())
    store.insert_samples("dev1", _baseline_rows(now, "18", 22.0))
    # 26.7 indoors (80 F) while it is 20 outside (68 F): nothing out there explains it
    monkeypatch.setattr(anomaly.weather, "outdoor", lambda s: _ctx(20.0, 55.0, now))
    got = [g for g in anomaly.evaluate(dev, {"18": 26.7}, settings, ts=now) if g["rule"] == "baseline"]
    assert got, "an unexplained warm room has to fire"
    assert "does not account for it" in got[0]["message"]
    assert "outside 20°C" in got[0]["message"]


def test_unsupported_swing_reaches_alert_sooner(settings, monkeypatch):
    dev = make_dev()
    now = int(time.time())
    store.insert_samples("dev1", _baseline_rows(now, "18", 22.0))
    monkeypatch.setattr(anomaly.weather, "outdoor", lambda s: _ctx(20.0, 55.0, now))
    got = [g for g in anomaly.evaluate(dev, {"18": 30.0}, settings, ts=now) if g["rule"] == "baseline"]
    assert got and got[0]["severity"] == "alert"


def test_humid_room_while_dry_outside_is_called_out(settings, monkeypatch):
    dev = make_dev()
    now = int(time.time())
    store.insert_samples("dev1", _baseline_rows(now, "19", 38.0))
    monkeypatch.setattr(anomaly.weather, "outdoor", lambda s: _ctx(20.0, 35.0, now))
    got = [g for g in anomaly.evaluate(dev, {"19": 55.0}, settings, ts=now) if g["rule"] == "baseline"]
    assert got and "does not account for it" in got[0]["message"]


def test_humid_room_while_wet_outside_is_expected(settings, monkeypatch):
    dev = make_dev()
    now = int(time.time())
    store.insert_samples("dev1", _baseline_rows(now, "19", 38.0))
    monkeypatch.setattr(anomaly.weather, "outdoor", lambda s: _ctx(20.0, 90.0, now))
    got = [g for g in anomaly.evaluate(dev, {"19": 55.0}, settings, ts=now) if g["rule"] == "baseline"]
    assert got == []


def test_baseline_hour_is_local_not_utc(settings):
    """The hour bucket has to be the hour a person would name, in the configured zone."""
    from zoneinfo import ZoneInfo

    settings.timezone = "America/New_York"
    dev = make_dev()
    zone = ZoneInfo("America/New_York")
    now = datetime.datetime(2026, 11, 16, 9, 30, tzinfo=zone)
    ts = int(now.timestamp())
    # 09:00 local on each of the last ten days, plus 09:00 UTC on one day (14:00 local)
    rows = []
    for d in range(1, 15):
        rows.append((int((now - datetime.timedelta(days=d)).replace(
            hour=9, minute=5).timestamp()), "18", 22.0, None))
    rows.append((int(datetime.datetime(2026, 11, 10, 9, 0, tzinfo=datetime.timezone.utc)
                      .timestamp()), "18", 40.0, None))
    store.insert_samples("dev1", rows)

    med, _, n = anomaly._baseline("dev1", "18", ts, 14, settings)
    assert n == 14, "the 09:00 UTC row is 14:00 local and must not join the 09:00 bucket"
    assert med == 22.0


def test_baseline_pools_both_sides_of_a_clock_change(settings):
    """Fall back repeats 01:00; the baseline must take both copies, not jump an hour."""
    from zoneinfo import ZoneInfo

    settings.timezone = "America/New_York"
    make_dev()
    zone = ZoneInfo("America/New_York")
    # 01:30 local on the day after the 2026-11-01 fall back, so 7 days and 14 back straddle it
    now = datetime.datetime(2026, 11, 8, 1, 30, tzinfo=zone)
    ts = int(now.timestamp())
    rows = []
    for d in range(1, 15):
        rows.append((int((now - datetime.timedelta(days=d)).replace(
            hour=1, minute=5).timestamp()), "18", 22.0, None))
    store.insert_samples("dev1", rows)

    _, _, n = anomaly._baseline("dev1", "18", ts, 14, settings)
    with store.telemetry("dev1", write=False) as con:
        hours = {r[0] for r in con.execute(
            "SELECT DISTINCT ((ts % 86400) / 3600) FROM samples WHERE dp='18'")}
    assert hours == {5, 6}, "one 01:00 is EDT, the other EST"
    assert n >= 13, "both offsets have to be pooled into the same baseline"
