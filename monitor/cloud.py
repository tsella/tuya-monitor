"""Tuya mobile-app API client: QR pairing and device discovery.

Contract and constants live in skill `tuya-app-api-local-key`; the signer is unforgiving,
so keep the parameter names and fixed values exactly as they are here.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request
import uuid

from . import config

STATE_FILE = config.DATA / "cloud_state.json"
USER_AGENT = "TuyaSmart/7.12.6 (Android)"


def _key() -> str:
    c = config.CLOUD
    return f"{c['cert']}_{c['secret2']}_{c['app_secret']}"


def _state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save(**kw) -> None:
    st = _state()
    st.update(kw)
    STATE_FILE.write_text(json.dumps(st))
    os.chmod(STATE_FILE, 0o600)


def _dice(h: str) -> str:
    return h[8:16] + h[0:8] + h[24:32] + h[16:24]


class CloudError(RuntimeError):
    def __init__(self, code: str, message: str = ""):
        super().__init__(f"{code} {message}".strip())
        self.code = code


def call(action: str, data: dict | None = None, query: dict | None = None,
         sid: str | None = None) -> dict:
    c = config.CLOUD
    st = _state()
    params = {
        "a": action,
        "clientId": c["client_id"],
        "deviceId": st.get("deviceId") or uuid.uuid4().hex,
        "os": "Android",
        "v": "1.0",
        "appVersion": c["app_version"],
        "ttid": c["ttid"],
        "lang": "en_US",
        "time": str(int(time.time() * 1000)),
        "requestId": str(uuid.uuid4()),
    }
    effective_sid = sid if sid is not None else st.get("sid")
    if effective_sid:
        params["sid"] = effective_sid
    if data:
        params["postData"] = json.dumps(data, separators=(",", ":"))

    signed = dict(params)
    if data:
        signed["postData"] = _dice(hashlib.md5(params["postData"].encode()).hexdigest())
    base = "||".join(f"{k}={signed[k]}" for k in sorted(signed) if k in config.SIGN_KEYS)
    params["sign"] = hmac.new(_key().encode(), base.encode(), hashlib.sha256).hexdigest()

    url = c["host"] + ("?" + urllib.parse.urlencode(query) if query else "")
    req = urllib.request.Request(url, data=urllib.parse.urlencode(params).encode(), headers={
        "User-Agent": USER_AGENT,
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=30) as res:
        out = json.loads(res.read().decode())

    _save(deviceId=params["deviceId"])
    if out.get("sid"):
        _save(sid=out["sid"])
    if not out.get("success"):
        raise CloudError(out.get("errorCode", "UNKNOWN"), out.get("errorMsg", ""))
    return out


def has_session() -> bool:
    return bool(_state().get("sid"))


def clear_session() -> None:
    st = _state()
    st.pop("sid", None)
    STATE_FILE.write_text(json.dumps(st))


# ------------------------------------------------------------------ pairing

def qr_start() -> str:
    out = call("tuya.m.user.qr.token.create", {"countryCode": "US", "lang": "en_US"})
    token = out["result"] if isinstance(out["result"], str) else out["result"]["token"]
    return token


def qr_poll(token: str) -> str | None:
    """Pending -> None. Approved -> sid (persisted)."""
    out = call("tuya.m.user.qr.token.user.get", {"token": token})
    res = out.get("result")
    if isinstance(res, dict) and res.get("sid"):
        _save(sid=res["sid"])
        return res["sid"]
    return None


def list_devices() -> list[dict]:
    homes = call("tuya.m.location.list").get("result") or []
    devices: list[dict] = []
    for home in homes:
        gid = home["groupId"]
        out = call("tuya.m.my.group.device.list", query={"gid": gid})
        for dev in out.get("result") or []:
            dev["_home"] = home.get("name", "")
            devices.append(dev)
    return devices
