import time

import pytest

from monitor import store


@pytest.fixture(autouse=True)
def clean():
    for f in (store.config.DATA).glob("*.db*"):
        f.unlink(missing_ok=True)


def test_insert_and_read_back():
    store.add_device("dev1", "Sensor", "192.0.2.5", "k" * 16, category="co2bj")
    now = int(time.time())
    store.insert_samples("dev1", [(now, "2", 500.0, "500")])
    rows = store.series("dev1", now - 5, now + 5)
    assert [(r["dp"], r["num"]) for r in rows] == [("2", 500.0)]


def test_duplicate_timestamp_is_ignored():
    store.add_device("dev1", "Sensor", "192.0.2.5", "k" * 16, category="co2bj")
    now = int(time.time())
    store.insert_samples("dev1", [(now, "2", 1.0, None)])
    store.insert_samples("dev1", [(now, "2", 9.0, None)])
    assert store.series("dev1", now - 1, now + 1)[0]["num"] == 1.0


def test_bucketed_series_aggregates():
    store.add_device("dev1", "Sensor", "192.0.2.5", "k" * 16, category="co2bj")
    base = 1_700_000_000
    store.insert_samples("dev1", [(base + i, "2", float(i), None) for i in range(10)])
    rows = store.series("dev1", base, base + 10, bucket=5)
    assert len(rows) == 2
    assert rows[0]["lo"] == 0.0 and rows[0]["hi"] == 4.0


def test_latest_picks_per_dp_max():
    store.add_device("dev1", "Sensor", "192.0.2.5", "k" * 16, category="co2bj")
    now = int(time.time())
    store.insert_samples("dev1", [(now - 5, "2", 1.0, None), (now, "2", 2.0, None)])
    assert store.latest("dev1")["2"]["value"] == 2.0


def test_prune_keeps_recent():
    store.add_device("dev1", "Sensor", "192.0.2.5", "k" * 16, category="co2bj")
    old = int(time.time()) - 40 * 86400
    store.insert_samples("dev1", [(old, "2", 1.0, None)])
    assert store.prune("dev1", 30) == 1


def test_prune_zero_keeps_everything():
    store.add_device("dev1", "Sensor", "192.0.2.5", "k" * 16, category="co2bj")
    old = int(time.time()) - 4000 * 86400
    store.insert_samples("dev1", [(old, "2", 1.0, None)])
    assert store.prune("dev1", 0) == 0
    assert store.series("dev1", old - 1, old + 1)[0]["num"] == 1.0


def test_alert_cooldown_lookup():
    store.add_device("dev1", "Sensor", "192.0.2.5", "k" * 16, category="co2bj")
    store.add_alert("dev1", "2", "static_high_warn", "warn", 1100, 1000, "hot")
    assert store.recent_alert("dev1", "2", "static_high_warn", 60) is not None
    assert store.recent_alert("dev1", "2", "other_rule", 60) is None


def test_enable_and_failure_counter():
    store.add_device("dev1", "Sensor", "192.0.2.5", "k" * 16, category="co2bj")
    assert store.record_fail("dev1", "boom") == 1
    assert store.record_fail("dev1", "boom") == 2
    store.record_ok("dev1")
    assert store.get_device("dev1")["failures"] == 0
