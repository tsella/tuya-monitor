from monitor import config


def test_defaults_expose_all_three_kinds():
    s = config.Settings()
    assert set(s.thresholds) == {"co2", "temp", "humidity"}
    assert s.thresholds["humidity"]["warn_high"] > s.thresholds["humidity"]["warn_low"]


def test_thresholds_are_overridable_from_file(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[thresholds.humidity]\nwarn_high = 55\nalert_high = 65\n")
    monkeypatch.setenv("TUYA_MONITOR_CONFIG", str(cfg))
    s = config.load()
    assert s.thresholds["humidity"]["warn_high"] == 55.0
    assert s.thresholds["humidity"]["alert_high"] == 65.0
    assert s.thresholds["humidity"]["warn_low"] == config.DEFAULT_THRESHOLDS["humidity"]["warn_low"]


def test_retention_defaults_to_uncapped():
    assert config.Settings().retention_days == 0


def test_temp_unit_override_and_fallback(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text('temp_unit = "f"\n[thresholds.co2]\nwarn = 900\n')
    monkeypatch.setenv("TUYA_MONITOR_CONFIG", str(cfg))
    assert config.load().temp_unit == "f"
    cfg.write_text('temp_unit = "kelvin"\n')
    assert config.load().temp_unit == "both"


def test_bind_and_port_override(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text('bind = "0.0.0.0"\nport = 9001\n')
    monkeypatch.setenv("TUYA_MONITOR_CONFIG", str(cfg))
    s = config.load()
    assert (s.bind, s.port) == ("0.0.0.0", 9001)


def test_conversions_round_trip():
    assert round(config.c_to_f(0), 1) == 32.0
    assert round(config.c_to_f(100), 1) == 212.0
    assert round(config.f_to_c(212), 1) == 100.0
