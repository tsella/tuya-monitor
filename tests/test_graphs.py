import time

import pytest

from monitor import config, graphs, store


@pytest.fixture(autouse=True)
def clean():
    for f in config.DATA.glob("*.db*"):
        f.unlink(missing_ok=True)
    for f in config.REPORTS.glob("*.png"):
        f.unlink(missing_ok=True)


def seed():
    dev = store.add_device("dev1", "Sensor", "192.0.2.5", "k" * 16, category="co2bj")
    now = int(time.time())
    rows = []
    for i in range(120):
        rows.append((now - 7200 + i * 60, "2", 500 + i, None))
        rows.append((now - 7200 + i * 60, "18", 22 + (i % 3), None))
        rows.append((now - 7200 + i * 60, "19", 35 + (i % 4), None))
    store.insert_samples("dev1", rows)
    return dev


def test_render_creates_png():
    dev = seed()
    now = int(time.time())
    path = graphs.render(dev, now - 7200, now)
    assert path.exists() and path.stat().st_size > 5000
    assert path.suffix == ".png"


def test_render_single_dp():
    dev = seed()
    now = int(time.time())
    path = graphs.render(dev, now - 3600, now, dps=["2"], title_suffix="daily")
    assert "daily" in path.name


def test_render_without_data_raises():
    dev = store.add_device("dev2", "Empty", "192.0.2.6", "k" * 16, category="co2bj")
    with pytest.raises(ValueError):
        graphs.render(dev, 1_600_000_000, 1_600_003_600)


def test_render_keeps_sane_canvas_and_paints_offscreen_thresholds():
    """Humidity sits far from its bounds; the arrows must not stretch the figure."""
    from PIL import Image
    dev = seed()
    now = int(time.time())
    path = graphs.render(dev, now - 1800, now, dps=["19"])
    w, h = Image.open(path).size
    assert 900 < w < 2000 and h < 1200


def test_temp_axis_is_dual_unit_by_default():
    dev = seed()
    now = int(time.time())
    path = graphs.render(dev, now - 1800, now, dps=["18"])
    assert path.exists()


def test_temp_can_render_in_fahrenheit_only(tmp_path):
    dev = seed()
    now = int(time.time())
    s = config.load()
    s.temp_unit = "f"
    path = graphs.render(dev, now - 1800, now, dps=["18"], out_dir=tmp_path, settings=s)
    assert path.exists() and path.parent == tmp_path


def test_prune_reports():
    dev = seed()
    now = int(time.time())
    path = graphs.render(dev, now - 3600, now)
    old = time.time() - 40 * 86400
    import os
    os.utime(path, (old, old))
    assert graphs.prune_reports(30) == 1


def test_weather_glyphs_are_mapped_from_wmo_codes():
    assert graphs._symbol(0) == "☀"
    assert graphs._symbol(3) == "☁"
    assert graphs._symbol(61) == "☂"
    assert graphs._symbol(75) == "❄"
    assert graphs._symbol(95) == "⚡"
    assert graphs._symbol(None) == ""


def test_aqi_colour_steps_with_severity():
    assert graphs._aqi_colour(10) != graphs._aqi_colour(50) != graphs._aqi_colour(120)


def _stub_history(monkeypatch):
    from monitor import weather

    now = int(time.time())
    monkeypatch.setattr(weather, "history", lambda s, a, b: {
        "ts": [now - 3600, now],
        "temperature_2m": [14.0, 12.0],
        "relative_humidity_2m": [60.0, 70.0],
        "precipitation": [0.0, 0.4],
        "weather_code": [61.0, 63.0],
        "aqi": [18.0, 46.0],
    })


def test_outside_series_gets_drawn(monkeypatch):
    dev = seed()
    _stub_history(monkeypatch)
    now = int(time.time())
    path = graphs.render(dev, now - 3600, now)
    assert path.exists()


def test_a_failed_history_lookup_still_renders(monkeypatch):
    from monitor import weather

    dev = seed()
    monkeypatch.setattr(weather, "history", lambda s, a, b: None)
    now = int(time.time())
    assert graphs.render(dev, now - 3600, now).exists()
