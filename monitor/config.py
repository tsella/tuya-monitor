"""Configuration: file, env, defaults. Nothing secret in the repo."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

HOME = Path(os.environ.get("HOME") or Path.home())
ROOT = Path(os.environ.get("TUYA_MONITOR_HOME", HOME / "projects/tuya-monitor"))
DATA = Path(os.environ.get("TUYA_MONITOR_DATA", ROOT / "data"))
REPORTS = Path(os.environ.get("TUYA_MONITOR_REPORTS", ROOT / "reports"))
CONFIG_FILE = Path(os.environ.get("TUYA_MONITOR_CONFIG", HOME / ".config/tuya-monitor/config.toml"))

# Tuya Smart 7.12.6 app identity (see skill tuya-app-api-local-key).
CLOUD = {
    "host": "https://a1.tuyaus.com/api.json",
    "client_id": "3cxxt3au9x33ytvq3h9j",
    "app_secret": "5gdtanjtf38vyxkqh87cjwfcqjhvjjqa",
    "secret2": "f3hd7pet4p83kemjdf5wqsa5tavrv579",
    "cert": "93:21:9F:C2:73:E2:20:0F:4A:DE:E5:F7:19:1D:C6:56:BA:2A:2D:7B:2F:F5:D2:4C:D5:5C:4B:61:55:00:1E:40",
    "app_version": "7.12.6",
    "ttid": "tuya",
}
SIGN_KEYS = {
    "a", "v", "lat", "lon", "lang", "deviceId", "appVersion", "ttid", "isH5", "h5Token",
    "os", "clientId", "postData", "time", "n4h5", "sid", "sp", "et", "requestId",
}

DP_META = {
    "co2bj": {
        "2": {"name": "CO₂", "unit": "ppm", "kind": "co2"},
        "18": {"name": "Temperature", "unit": "°C", "kind": "temp"},
        "19": {"name": "Humidity", "unit": "%", "kind": "humidity"},
        "101": {"name": "Unit", "unit": "", "kind": "enum"},
    }
}

# Defaults shipped with the code; [thresholds.<kind>] in the config file overrides any of
# them field by field. Values are in the DP's own unit (CO₂ ppm, °C, % RH).
DEFAULT_THRESHOLDS = {
    "co2": {"warn": 1000.0, "alert": 1500.0, "floor": 120.0},
    "temp": {"warn_low": 5.0, "warn_high": 30.0, "alert_low": 0.0, "alert_high": 40.0, "floor": 1.5},
    "humidity": {"warn_low": 30.0, "warn_high": 60.0, "alert_low": 20.0, "alert_high": 70.0, "floor": 5.0},
}
THRESHOLDS = DEFAULT_THRESHOLDS  # backwards-compatible alias

TEMP_UNITS = ("c", "f", "both")


def c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


def f_to_c(f: float) -> float:
    return (f - 32) * 5 / 9


@dataclass
class Settings:
    bind: str = "127.0.0.1"
    port: int = 8765
    api_key: str = ""
    poll_interval: float = 30.0
    retention_days: int = 0          # 0 = keep everything
    report_retention_days: int = 30
    cooldown_min: int = 30
    anomaly_enabled: bool = True
    baseline_days: int = 7
    temp_unit: str = "both"          # c | f | both
    timezone: str = "America/New_York"
    data_dir: Path = field(default_factory=lambda: DATA)
    reports_dir: Path = field(default_factory=lambda: REPORTS)
    thresholds: dict = field(default_factory=lambda: {k: dict(v) for k, v in DEFAULT_THRESHOLDS.items()})


def load() -> Settings:
    cfg: dict = {}
    config_file = Path(os.environ.get("TUYA_MONITOR_CONFIG", CONFIG_FILE))
    if config_file.exists():
        with open(config_file, "rb") as fh:
            cfg = tomllib.load(fh)
    s = Settings()
    for key in ("bind", "port", "api_key", "poll_interval", "retention_days",
                "report_retention_days", "cooldown_min", "anomaly_enabled",
                "baseline_days", "temp_unit", "timezone"):
        if key in cfg:
            setattr(s, key, cfg[key])
        env = os.environ.get("TUYA_MONITOR_" + key.upper())
        if env:
            cur = getattr(s, key)
            setattr(s, key, type(cur)(env) if not isinstance(cur, bool) else env.lower() in ("1", "true", "yes"))
    for kind, values in (cfg.get("thresholds") or {}).items():
        merged = dict(s.thresholds.get(kind, {}))
        merged.update({k: float(v) for k, v in values.items()})
        s.thresholds[kind] = merged
    if s.temp_unit not in TEMP_UNITS:
        s.temp_unit = "both"
    return s


def ensure_dirs() -> None:
    for d, mode in ((DATA, 0o700), (REPORTS, 0o755)):
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, mode)
