"""Local Tuya protocol wrapper and value normalisation."""
from __future__ import annotations

import time
from typing import Any

import tinytuya

from . import config

TIMEOUT = 6.0


def _device(ip: str, dev_id: str, key: str, version: float) -> tinytuya.Device:
    d = tinytuya.Device(dev_id, ip, key, version=version)
    d.set_socketPersistent(False)
    d.set_socketTimeout(TIMEOUT)
    return d


def probe_version(ip: str, dev_id: str, key: str) -> float:
    """Return the first protocol version that yields dps. 904 = wrong framing, 914 = key/version."""
    errors = []
    for version in (3.3, 3.4, 3.5):
        try:
            out = _device(ip, dev_id, key, version).status()
            if isinstance(out, dict) and out.get("dps"):
                return version
            errors.append(f"{version}: {out.get('Err') if isinstance(out, dict) else out}")
        except Exception as exc:  # noqa: BLE001 - transport errors are all "no"
            errors.append(f"{version}: {exc}")
    raise RuntimeError("no protocol version accepted the key (" + "; ".join(errors) + ")")


def read(ip: str, dev_id: str, key: str, version: float) -> dict[str, Any]:
    out = _device(ip, dev_id, key, version).status()
    if not isinstance(out, dict):
        raise RuntimeError(f"unexpected response: {out!r}")
    if out.get("Err"):
        raise RuntimeError(f"{out.get('Err')} {out.get('Error', '')}".strip())
    if "dps" not in out:
        raise RuntimeError(f"no dps in response: {out}")
    return out["dps"]


def rows_from_dps(dps: dict[str, Any], ts: int | None = None) -> list[tuple[int, str, float | None, str | None]]:
    """Split values into numeric and text columns so SQL can aggregate cleanly."""
    ts = ts or int(time.time())
    rows = []
    for dp, value in dps.items():
        if isinstance(value, bool):
            rows.append((ts, dp, float(value), None))
        elif isinstance(value, (int, float)):
            rows.append((ts, dp, float(value), None))
        elif isinstance(value, str):
            try:
                rows.append((ts, dp, float(value), value))
            except ValueError:
                rows.append((ts, dp, None, value))
        else:
            rows.append((ts, dp, None, str(value)))
    return rows


def meta_for(category: str, dp: str) -> dict:
    return config.DP_META.get(category, {}).get(dp, {})
