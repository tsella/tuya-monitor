"""Command line: init, pair, verify, collect, serve, graph, report, notify, prune."""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
from pathlib import Path

from . import anomaly, cloud, collector, config, graphs, realtime, store


def _log(level: str) -> None:
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def cmd_init(args) -> int:
    config.ensure_dirs()
    with store.registry():
        pass
    print(f"initialised under {config.ROOT} (data={config.DATA})")
    return 0


def cmd_pair(args) -> int:
    config.ensure_dirs()
    token = cloud.qr_start()
    import qrcode
    path = config.DATA / "pair.png"
    qrcode.make("tuyaSmart--qrLogin?token=" + token).save(path)
    print(f"Scan {path} with the Tuya app (Me -> scan), then wait:")
    for _ in range(150):
        if cloud.qr_poll(token):
            break
        time.sleep(4)
    else:
        print("timed out"); return 1
    found = 0
    for dev in cloud.list_devices():
        ip = (dev.get("ip") or "").strip()
        if not ip:
            continue
        store.add_device(dev["devId"], dev.get("name") or dev["devId"], ip, dev.get("localKey", ""),
                         category=dev.get("category", ""), product_id=dev.get("productId", ""))
        print(f"imported {dev['devId']} {dev.get('name')} ({ip}) — key stored, disabled")
        found += 1
    print(f"{found} device(s) imported; run `monitor verify <dev_id>` next")
    return 0 if found else 1


def cmd_import_local(args) -> int:
    """Seed a device from an already-recovered device.json (no cloud call)."""
    cfg = json.loads(Path(args.file).read_text())
    dev = store.add_device(cfg["devId"], cfg.get("name", cfg["devId"]), cfg["ip"], cfg["localKey"],
                           category=cfg.get("category", ""), product_id=cfg.get("productId", ""),
                           version=float(cfg.get("version", 0)))
    store.set_enabled(dev["dev_id"], True)
    print(f"imported {dev['dev_id']} ({dev['ip']}) enabled at v{cfg.get('version')}")
    return 0


def cmd_devices(args) -> int:
    for dev in store.list_devices():
        print(f"{dev['dev_id']}  {dev['name'][:24]:24} {dev['ip']:15} v{dev['version']} "
              f"{'on' if dev['enabled'] else 'off'}  fail={dev['failures']}  "
              f"seen={dev['last_seen'] or '-'}  {dev['last_error'] or ''}")
    return 0


def cmd_verify(args) -> int:
    dev = store.get_device(args.dev_id)
    if not dev:
        print("unknown device"); return 1
    version = realtime.probe_version(dev["ip"], dev["dev_id"], dev["local_key"])
    dps = realtime.read(dev["ip"], dev["dev_id"], dev["local_key"], version)
    store.set_enabled(dev["dev_id"], True)
    with store.registry() as con:
        con.execute("UPDATE devices SET version=? WHERE dev_id=?", (version, dev["dev_id"]))
    print(f"ok at v{version}: {json.dumps(dps)}")
    return 0


def cmd_collect(args) -> int:
    settings = config.load()
    def announce(alert: dict) -> None:
        print(f"ANOMALY {alert['severity']}: {alert['message']}", flush=True)
    sup = collector.Supervisor(settings, announce)
    try:
        sup.run()
    except KeyboardInterrupt:
        sup.stop()
    return 0


def cmd_serve(args) -> int:
    import uvicorn
    settings = config.load()
    uvicorn.run("monitor.api:app", host=args.bind or settings.bind, port=args.port or settings.port,
                log_level="info", workers=1)
    return 0


def cmd_graph(args) -> int:
    dev = store.get_device(args.dev_id)
    if not dev:
        print("unknown device"); return 1
    settings = config.load()
    end = int(args.end) if args.end else int(time.time())
    start = int(args.start) if args.start else end - int(float(args.hours) * 3600)
    path = graphs.render(dev, start, end, dps=args.dp.split(",") if args.dp else None,
                         title_suffix=args.label or "", settings=settings)
    print(path)
    return 0


def cmd_report(args) -> int:
    """Render the scheduled daily/weekly pack. Prints one line per file."""
    settings = config.load()
    now = int(time.time())
    windows = {"daily": (now - 86400, now), "weekly": (now - 7 * 86400, now)}
    start, end = windows[args.kind]
    written = []
    for dev in store.list_devices():
        if not dev["enabled"]:
            continue
        try:
            written.append(str(graphs.render(dev, start, end, title_suffix=args.kind, settings=settings)))
        except ValueError:
            continue
    graphs.prune_reports(settings.report_retention_days)
    print("\n".join(written) or "no data")
    return 0 if written else 1


def cmd_notify(args) -> int:
    fresh = store.fresh_alerts(limit=args.limit)
    quiet = [r for r in store.open_alerts(limit=args.limit)
             if r.get("notified_at") and not r.get("acked_at")]
    if args.json:
        print(json.dumps({"fresh": fresh, "quiet": quiet}, default=str))
    else:
        for r in fresh:
            print(f"[{r['severity'].upper()}] {r['message']} ({time.strftime('%m-%d %H:%M', time.localtime(r['ts']))})")
        if not fresh and not args.include_quiet:
            print("[SILENT]")
        for r in quiet if args.include_quiet else []:
            print(f"(open) [{r['severity'].upper()}] {r['message']}")
    store.mark_notified([r["id"] for r in fresh])
    return 0


def cmd_prune(args) -> int:
    settings = config.load()
    n = sum(store.prune(d["dev_id"], settings.retention_days) for d in store.list_devices())
    r = graphs.prune_reports(settings.report_retention_days)
    print(f"pruned {n} samples, {r} reports")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="monitor")
    p.add_argument("--log", default="info")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(fn=cmd_init)
    sub.add_parser("pair").set_defaults(fn=cmd_pair)
    imp = sub.add_parser("import-local"); imp.add_argument("file"); imp.set_defaults(fn=cmd_import_local)
    sub.add_parser("devices").set_defaults(fn=cmd_devices)
    v = sub.add_parser("verify"); v.add_argument("dev_id"); v.set_defaults(fn=cmd_verify)
    sub.add_parser("collect").set_defaults(fn=cmd_collect)
    s = sub.add_parser("serve"); s.add_argument("--bind"); s.add_argument("--port", type=int)
    s.set_defaults(fn=cmd_serve)
    g = sub.add_parser("graph"); g.add_argument("dev_id"); g.add_argument("--start")
    g.add_argument("--end"); g.add_argument("--hours", default="24"); g.add_argument("--dp")
    g.add_argument("--label"); g.set_defaults(fn=cmd_graph)
    r = sub.add_parser("report"); r.add_argument("kind", choices=["daily", "weekly"]); r.set_defaults(fn=cmd_report)
    n = sub.add_parser("notify"); n.add_argument("--json", action="store_true")
    n.add_argument("--limit", type=int, default=50); n.add_argument("--include-quiet", action="store_true")
    n.set_defaults(fn=cmd_notify)
    sub.add_parser("prune").set_defaults(fn=cmd_prune)

    args = p.parse_args(argv)
    _log(args.log)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
