"""Regression tests for docs/state-machines.md. Each one failed before its fix."""
import time

import pytest
from fastapi.testclient import TestClient

from monitor import cloud, config, collector, store


@pytest.fixture(autouse=True)
def clean():
    for f in config.DATA.glob("*.db*"):
        f.unlink(missing_ok=True)


def client():
    from monitor.api import app
    return TestClient(app)


# IS-1 --------------------------------------------------------------------

def test_repairing_does_not_disable_a_live_device():
    store.add_device("dev1", "Sensor", "192.0.2.5", "k" * 16, category="co2bj")
    store.set_enabled("dev1", True)
    store.add_device("dev1", "Sensor", "192.0.2.5", "j" * 16, category="co2bj")  # re-pair import
    assert store.get_device("dev1")["enabled"] == 1


def test_import_keeps_version_and_metadata():
    store.add_device("dev1", "Sensor", "192.0.2.5", "k" * 16, category="co2bj")
    with store.registry() as con:
        con.execute("UPDATE devices SET version=3.5, dp_meta=? WHERE dev_id='dev1'",
                    ('{"2": {"name": "CO2", "unit": "ppm", "kind": "co2"}}',))
    store.add_device("dev1", "Sensor", "192.0.2.5", "k" * 16, category="co2bj")
    dev = store.get_device("dev1")
    assert dev["version"] == 3.5
    assert dev["dp_meta"]["2"]["name"] == "CO2"


# IS-2 --------------------------------------------------------------------

def test_approved_session_does_not_reimport(monkeypatch):
    calls = {"n": 0}
    real = store.add_device

    def counted(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    monkeypatch.setattr(store, "add_device", counted)
    monkeypatch.setattr(cloud, "qr_start", lambda: "tok")
    monkeypatch.setattr(cloud, "qr_poll", lambda t: "sid123")
    monkeypatch.setattr(cloud, "list_devices", lambda: [
        {"devId": "dev1", "name": "S", "ip": "192.0.2.5", "localKey": "k" * 16,
         "category": "co2bj", "productId": "p"}])
    c = client()
    sid = c.post("/pair").json()["session"]
    assert c.get(f"/pair/{sid}").json()["status"] == "approved"
    c.get(f"/pair/{sid}")
    assert calls["n"] == 1


def test_expired_session_is_terminal(monkeypatch):
    monkeypatch.setattr(cloud, "qr_start", lambda: "tok")
    monkeypatch.setattr(cloud, "qr_poll", lambda t: "sid123")
    monkeypatch.setattr(cloud, "list_devices", lambda: [])
    c = client()
    sid = c.post("/pair").json()["session"]
    with store.registry() as con:
        con.execute("UPDATE pair_sessions SET expires=? WHERE id=?", (int(time.time()) - 1, sid))
    assert c.get(f"/pair/{sid}").json()["status"] == "expired"
    assert c.get(f"/pair/{sid}").json()["status"] == "expired"


def test_pair_survives_a_non_cloud_exception(monkeypatch):
    monkeypatch.setattr(cloud, "qr_start", lambda: "tok")
    monkeypatch.setattr(cloud, "qr_poll", lambda t: "sid123")

    def boom():
        raise TimeoutError("socket closed")
    monkeypatch.setattr(cloud, "list_devices", boom)
    c = client()
    sid = c.post("/pair").json()["session"]
    r = c.get(f"/pair/{sid}")
    assert r.status_code == 200 and r.json()["status"] == "failed"


# IS-3 --------------------------------------------------------------------

def test_alerts_endpoint_keeps_acked_rows_out_of_the_open_set():
    store.add_device("dev1", "Sensor", "192.0.2.5", "k" * 16, category="co2bj")
    alert_id = store.add_alert("dev1", "2", "r", "warn", 1, 0, "msg")
    client().post(f"/alerts/{alert_id}/ack")
    assert [a["id"] for a in client().get("/alerts").json()] == []


def test_ack_is_idempotent():
    store.add_device("dev1", "Sensor", "192.0.2.5", "k" * 16, category="co2bj")
    alert_id = store.add_alert("dev1", "2", "r", "warn", 1, 0, "msg")
    c = client()
    assert c.post(f"/alerts/{alert_id}/ack").json()["acked"] is True
    assert c.post(f"/alerts/{alert_id}/ack").json()["acked"] is True


def test_unknown_ack_is_404():
    assert client().post("/alerts/9999/ack").status_code == 404


def test_notifier_only_sees_unnotified_rows():
    store.add_device("dev1", "Sensor", "192.0.2.5", "k" * 16, category="co2bj")
    store.add_alert("dev1", "2", "r", "warn", 1, 0, "one")
    fresh = store.fresh_alerts()
    assert [a["message"] for a in fresh] == ["one"]
    store.mark_notified([a["id"] for a in fresh])
    assert store.fresh_alerts() == []


# IS-4 --------------------------------------------------------------------

def test_series_is_range_capped():
    store.add_device("dev1", "Sensor", "192.0.2.5", "k" * 16, category="co2bj")
    now = int(time.time())
    r = client().get(f"/devices/dev1/series?start={now - 200 * 86400}&end={now}")
    assert r.status_code == 400


def test_series_long_range_allowed_when_bucketed():
    store.add_device("dev1", "Sensor", "192.0.2.5", "k" * 16, category="co2bj")
    now = int(time.time())
    r = client().get(f"/devices/dev1/series?start={now - 200 * 86400}&end={now}&bucket=3600")
    assert r.status_code == 200


def test_prune_scheduler_runs_once_per_period():
    s = collector.Supervisor(config.Settings())
    t0 = 1_000_000.0
    assert s.prune_due(t0) is True
    assert s.prune_due(t0 + 60) is False
    assert s.prune_due(t0 + collector.PRUNE_PERIOD) is True


# IS-5 --------------------------------------------------------------------

def test_disabling_keeps_the_reason():
    store.add_device("dev1", "Sensor", "192.0.2.5", "k" * 16, category="co2bj")
    store.record_fail("dev1", "914 key/version mismatch")
    store.set_enabled("dev1", False)
    dev = store.get_device("dev1")
    assert dev["enabled"] == 0
    assert "914" in (dev["last_error"] or "")
