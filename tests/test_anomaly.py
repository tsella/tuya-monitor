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
