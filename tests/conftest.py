import os
import tempfile
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="tuya-monitor-test-"))
os.environ["TUYA_MONITOR_DATA"] = str(ROOT / "data")
os.environ["TUYA_MONITOR_REPORTS"] = str(ROOT / "reports")
os.environ["TUYA_MONITOR_HOME"] = str(ROOT)
os.environ["TUYA_MONITOR_CONFIG"] = str(ROOT / "config.toml")
os.environ["TUYA_MONITOR_ANOMALY_ENABLED"] = "1"
os.environ["TUYA_MONITOR_COOLDOWN_MIN"] = "30"
