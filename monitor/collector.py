"""Collector: one worker thread per enabled device, backoff, write-through anomalies."""
from __future__ import annotations

import logging
import random
import threading
import time
from typing import Callable

from . import anomaly, config, realtime, store

log = logging.getLogger("monitor.collector")
DISABLE_AFTER = 5
PRUNE_PERIOD = 600.0


class Worker(threading.Thread):
    def __init__(self, dev: dict, settings: config.Settings,
                 on_anomaly: Callable[[dict], None] | None = None):
        super().__init__(name=f"poll-{dev['dev_id'][:8]}", daemon=True)
        self.dev = dev
        self.settings = settings
        self.on_anomaly = on_anomaly or (lambda _a: None)
        self.stop_event = threading.Event()

    def run(self) -> None:  # noqa: D401 - thread body
        delay = 0.5
        while not self.stop_event.is_set():
            started = time.time()
            dev = store.get_device(self.dev["dev_id"])
            if not dev or not dev.get("enabled"):
                log.info("%s disabled, worker exits", self.dev["dev_id"])
                return
            try:
                dps = realtime.read(dev["ip"], dev["dev_id"], dev["local_key"], dev["version"])
                store.insert_samples(dev["dev_id"], realtime.rows_from_dps(dps))
                store.record_ok(dev["dev_id"])
                for alert in anomaly.evaluate(dev, dps, self.settings):
                    log.warning("anomaly %s", alert["message"])
                    self.on_anomaly(alert)
                delay = max(self.settings.poll_interval, 1.0)
            except Exception as exc:  # noqa: BLE001 - every transport error is retryable
                failures = store.record_fail(dev["dev_id"], str(exc))
                log.warning("%s poll failed (%d): %s", dev["dev_id"], failures, exc)
                if failures >= DISABLE_AFTER:
                    store.set_enabled(dev["dev_id"], False)
                    store.add_alert(dev["dev_id"], "", "device_offline", "alert", None, None,
                                    f"{dev['name']} unreachable after {failures} attempts: {exc}")
                    return
                delay = min(delay * 2, 60.0) * (0.85 + random.random() * 0.3)
            self.stop_event.wait(max(delay - (time.time() - started), 0.5))

    def stop(self) -> None:
        self.stop_event.set()


class Supervisor:
    def __init__(self, settings: config.Settings, on_anomaly: Callable[[dict], None] | None = None):
        self.settings = settings
        self.on_anomaly = on_anomaly
        self.workers: dict[str, Worker] = {}
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self._last_prune = 0.0

    def prune_due(self, now: float | None = None) -> bool:
        """Elapsed-time gate, independent of the poll interval."""
        now = time.time() if now is None else now
        if now - self._last_prune >= PRUNE_PERIOD:
            self._last_prune = now
            return True
        return False

    def reap(self) -> None:
        with self.lock:
            for dev_id, w in list(self.workers.items()):
                if not w.is_alive():
                    self.workers.pop(dev_id, None)

    def sync(self) -> None:
        wanted = {d["dev_id"]: d for d in store.list_devices() if d["enabled"]}
        self.reap()
        with self.lock:
            for dev_id in list(self.workers):
                if dev_id not in wanted:
                    self.workers.pop(dev_id).stop()
            for dev_id, dev in wanted.items():
                if dev_id not in self.workers:
                    log.info("starting worker for %s", dev_id)
                    w = Worker(dev, self.settings, self.on_anomaly)
                    self.workers[dev_id] = w
                    w.start()

    def run(self) -> None:
        log.info("supervisor up (interval %.1fs)", self.settings.poll_interval)
        while not self.stop_event.is_set():
            self.sync()
            if self.prune_due():
                for dev in store.list_devices():
                    store.prune(dev["dev_id"], self.settings.retention_days)
            self.stop_event.wait(5)

    def stop(self) -> None:
        self.stop_event.set()
        with self.lock:
            for w in self.workers.values():
                w.stop()
            self.workers.clear()


def poll_once(dev: dict, settings: config.Settings) -> dict:
    """One synchronous read; used by the API and by tests."""
    dps = realtime.read(dev["ip"], dev["dev_id"], dev["local_key"], dev["version"])
    store.insert_samples(dev["dev_id"], realtime.rows_from_dps(dps))
    store.record_ok(dev["dev_id"])
    return dps
