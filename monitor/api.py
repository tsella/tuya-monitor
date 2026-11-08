"""HTTP API. Localhost-bound; optional bearer key; pairing is a two-step session."""
from __future__ import annotations

import io
import secrets
import time
import uuid
from typing import Optional

import qrcode
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import FileResponse

from . import anomaly, cloud, config, collector, graphs, realtime, store

settings = config.load()
app = FastAPI(title="tuya-monitor", version="1.0.0", docs_url="/docs" if not settings.api_key else None)


def auth(x_api_key: Optional[str] = Header(default=None)) -> None:
    if settings.api_key and not (x_api_key and secrets.compare_digest(x_api_key, settings.api_key)):
        raise HTTPException(401, "bad api key")


def _dev(dev_id: str) -> dict:
    dev = store.get_device(dev_id)
    if not dev:
        raise HTTPException(404, "unknown device")
    return dev


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "ts": int(time.time()), "devices": len(store.list_devices())}


@app.get("/devices", dependencies=[Depends(auth)])
def devices() -> list[dict]:
    out = []
    for dev in store.list_devices():
        dev = dict(dev)
        dev["local_key"] = "••••" if dev["local_key"] else ""
        dev["latest"] = store.latest(dev["dev_id"])
        out.append(dev)
    return out


def _transition(session: str, status: str, detail: str, only_if_pending: bool = False) -> bool:
    """Short write. Returns False when the guard rejected it (someone else got there first)."""
    with store.registry() as con:
        sql = "UPDATE pair_sessions SET status=?, detail=? WHERE id=?"
        if only_if_pending:
            sql += " AND status='pending'"
        return con.execute(sql, (status, detail, session)).rowcount > 0


@app.post("/pair", dependencies=[Depends(auth)])
def pair_start() -> dict:
    try:
        token = cloud.qr_start()
    except cloud.CloudError as exc:
        raise HTTPException(502, f"cloud rejected: {exc.code}") from exc
    sid = uuid.uuid4().hex[:12]
    now = int(time.time())
    with store.registry() as con:
        con.execute("DELETE FROM pair_sessions WHERE created < ?", (now - 86400,))
        con.execute("INSERT INTO pair_sessions(id, token, created, expires) VALUES (?,?,?,?)",
                    (sid, token, now, now + 600))
    return {"session": sid, "qr_url": f"/pair/{sid}/qr.png", "status": "pending",
            "expires_in": 600}


@app.get("/pair/{session}/qr.png", dependencies=[Depends(auth)])
def pair_qr(session: str) -> Response:
    with store.registry(write=False) as con:
        row = con.execute("SELECT token FROM pair_sessions WHERE id=?", (session,)).fetchone()
    if not row:
        raise HTTPException(404, "unknown session")
    img = qrcode.make("tuyaSmart--qrLogin?token=" + row["token"])
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(buf.getvalue(), media_type="image/png")


@app.get("/pair/{session}", dependencies=[Depends(auth)])
def pair_status(session: str) -> dict:
    # No transaction is held across a network call: one short read, decide, one short write.
    with store.registry(write=False) as con:
        row = con.execute("SELECT * FROM pair_sessions WHERE id=?", (session,)).fetchone()
    if not row:
        raise HTTPException(404, "unknown session")
    if row["status"] != "pending":
        return {"status": row["status"], "detail": row["detail"]}
    if row["expires"] < time.time():
        _transition(session, "expired", "qr token expired")
        return {"status": "expired"}

    try:
        sid = cloud.qr_poll(row["token"])
    except cloud.CloudError as exc:
        raise HTTPException(502, f"cloud rejected: {exc.code}") from exc
    if not sid:
        return {"status": "pending"}

    try:
        found = cloud.list_devices()
    except Exception as exc:  # noqa: BLE001 - timeouts and JSON errors are the same outcome
        _transition(session, "failed", type(exc).__name__)
        return {"status": "failed", "detail": type(exc).__name__}

    imported = []
    for dev in found:
        ip = (dev.get("ip") or "").strip()
        if not ip:
            continue
        row_dev = store.add_device(dev["devId"], dev.get("name") or dev["devId"], ip,
                                   dev.get("localKey", ""), category=dev.get("category", ""),
                                   product_id=dev.get("productId", ""), enabled=None)
        imported.append({"dev_id": row_dev["dev_id"], "name": row_dev["name"],
                         "category": row_dev["category"], "enabled": row_dev["enabled"]})

    detail = f"{len(imported)} devices"
    if not _transition(session, "approved", detail, only_if_pending=True):
        # a concurrent poll approved it first; report that result instead of importing twice
        with store.registry(write=False) as con:
            again = con.execute("SELECT status, detail FROM pair_sessions WHERE id=?",
                                (session,)).fetchone()
        return {"status": again["status"], "detail": again["detail"]}
    return {"status": "approved", "devices": imported, "imported": len(imported)}


@app.post("/devices/{dev_id}/verify", dependencies=[Depends(auth)])
def verify(dev_id: str) -> dict:
    dev = _dev(dev_id)
    if not dev["local_key"]:
        raise HTTPException(409, "no local key stored; re-pair the device")
    try:
        version = realtime.probe_version(dev["ip"], dev_id, dev["local_key"])
        dps = realtime.read(dev["ip"], dev_id, dev["local_key"], version)
    except Exception as exc:  # noqa: BLE001
        store.set_enabled(dev_id, False)
        raise HTTPException(502, f"device rejected the key: {exc}") from exc
    with store.registry() as con:
        con.execute("UPDATE devices SET version=?, enabled=1, failures=0, last_error=NULL WHERE dev_id=?",
                    (version, dev_id))
    return {"dev_id": dev_id, "version": version, "dps": dps}


@app.post("/devices/{dev_id}/poll", dependencies=[Depends(auth)])
def poll_now(dev_id: str) -> dict:
    dev = _dev(dev_id)
    try:
        dps = collector.poll_once(dev, settings)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, str(exc)) from exc
    alerts = anomaly.evaluate(dev, dps, settings)
    return {"dps": dps, "alerts": alerts}


@app.get("/devices/{dev_id}/latest", dependencies=[Depends(auth)])
def latest(dev_id: str) -> dict:
    dev = _dev(dev_id)
    out = store.latest(dev_id)
    for dp, item in out.items():
        if realtime.meta_for(dev["category"], dp).get("kind") == "temp" and item["value"] is not None:
            item["f"] = round(config.c_to_f(float(item["value"])), 1)
    return out


@app.get("/devices/{dev_id}/series", dependencies=[Depends(auth)])
def series(dev_id: str, start: int, end: int, dp: Optional[str] = None,
           bucket: int = 0, units: str = "c") -> list[dict]:
    dev = _dev(dev_id)
    if start >= end:
        raise HTTPException(400, "end must be after start")
    if bucket <= 0 and end - start > 90 * 86400:
        raise HTTPException(400, "raw ranges are capped at 90 days; pass bucket for longer")
    rows = store.series(dev_id, start, end, dp=dp, bucket=bucket)
    if units == "f":
        for row in rows:
            if realtime.meta_for(dev["category"], row["dp"]).get("kind") != "temp":
                continue
            for key in ("num", "lo", "hi", "avg"):
                if row.get(key) is not None:
                    row[key] = round(config.c_to_f(float(row[key])), 1)
    return rows


@app.get("/devices/{dev_id}/graph.png", dependencies=[Depends(auth)])
def graph(dev_id: str, start: int, end: int, dp: str | None = Query(default=None)) -> FileResponse:
    dev = _dev(dev_id)
    if end <= start:
        raise HTTPException(400, "end must be after start")
    if end - start > 90 * 86400:
        raise HTTPException(400, "range capped at 90 days")
    try:
        path = graphs.render(dev, start, end, dps=dp.split(",") if dp else None, settings=settings)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(path, media_type="image/png", filename=path.name)


@app.get("/settings", dependencies=[Depends(auth)])
def get_settings() -> dict:
    return {"bind": settings.bind, "port": settings.port, "poll_interval": settings.poll_interval,
            "retention_days": settings.retention_days, "temp_unit": settings.temp_unit,
            "cooldown_min": settings.cooldown_min, "anomaly_enabled": settings.anomaly_enabled,
            "thresholds": settings.thresholds}


@app.get("/alerts", dependencies=[Depends(auth)])
def alerts(limit: int = 100) -> list[dict]:
    return store.open_alerts(limit)


@app.post("/alerts/{alert_id}/ack", dependencies=[Depends(auth)])
def alert_ack(alert_id: int) -> dict:
    if not store.ack(alert_id):
        raise HTTPException(404, "unknown alert")
    return {"acked": True}


@app.get("/reports", dependencies=[Depends(auth)])
def reports() -> list[dict]:
    return [{"name": f.name, "bytes": f.stat().st_size, "mtime": int(f.stat().st_mtime)}
            for f in sorted(config.REPORTS.glob("*.png"))]
