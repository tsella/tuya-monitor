import time

import pytest
from fastapi.testclient import TestClient

from monitor import config, store


@pytest.fixture(autouse=True)
def clean():
    for f in config.DATA.glob("*.db*"):
        f.unlink(missing_ok=True)


def client():
    from monitor.api import app
    return TestClient(app)


def test_healthz():
    assert client().get("/healthz").json()["ok"] is True


def test_devices_hides_key():
    store.add_device("dev1", "Sensor", "192.0.2.5", "abcdefghijklmnop", category="co2bj")
    row = client().get("/devices").json()[0]
    assert row["dev_id"] == "dev1" and row["local_key"].startswith("•")


def test_unknown_device_404():
    assert client().get("/devices/nope/series?start=1&end=2").status_code == 404


def test_series_returns_rows():
    store.add_device("dev1", "Sensor", "192.0.2.5", "k" * 16, category="co2bj")
    now = int(time.time())
    store.insert_samples("dev1", [(now, "2", 500.0, None)])
    rows = client().get(f"/devices/dev1/series?start={now-5}&end={now+5}").json()
    assert rows[0]["num"] == 500.0


def test_graph_endpoint_returns_png():
    store.add_device("dev1", "Sensor", "192.0.2.5", "k" * 16, category="co2bj")
    now = int(time.time())
    store.insert_samples("dev1", [(now - i * 60, "2", 500.0 + i, None) for i in range(30)])
    res = client().get(f"/devices/dev1/graph.png?start={now-3600}&end={now}")
    assert res.status_code == 200 and res.headers["content-type"] == "image/png"


def test_alerts_endpoint_and_ack():
    store.add_device("dev1", "Sensor", "192.0.2.5", "k" * 16, category="co2bj")
    alert_id = store.add_alert("dev1", "2", "r", "warn", 1, 0, "msg")
    assert client().get("/alerts").json()[0]["message"] == "msg"
    assert client().post(f"/alerts/{alert_id}/ack").json()["acked"] is True


def test_graph_rejects_bad_range():
    store.add_device("dev1", "Sensor", "192.0.2.5", "k" * 16, category="co2bj")
    assert client().get("/devices/dev1/graph.png?start=10&end=5").status_code == 400


def test_settings_expose_the_anti_noise_knobs():
    body = client().get("/settings").json()
    assert body["confirm_samples"] >= 1
    assert body["cooldown_min"] >= 30
