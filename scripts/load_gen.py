#!/usr/bin/env python3
"""Steady synthetic traffic against /authorize, with a running tally of outcomes.

Usage: python scripts/load_gen.py --url http://127.0.0.1:8099 --rps 20 --duration 60
Importable: LoadGen(url, rps).start() / .stop() / .snapshot() for use in drills.
"""
import argparse
import json
import random
import threading
import time
import urllib.error
import urllib.request


class LoadGen:
    def __init__(self, url, rps=20, workers=4):
        self.url = url.rstrip("/") + "/authorize"
        self.interval = workers / float(rps)
        self.workers = workers
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.events = []  # (timestamp, http_status_or_0, latency_ms)
        self._threads = []

    def _one(self):
        body = json.dumps({
            "card_token": f"card-{random.randint(1, 5000)}",
            "amount": round(random.uniform(1, 400), 2),
            "merchant_id": f"m-{random.randint(1, 20)}",
        }).encode()
        req = urllib.request.Request(self.url, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        start = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=2.0) as r:
                status = r.status
        except urllib.error.HTTPError as exc:
            status = exc.code
        except Exception:
            status = 0  # connection refused / timeout
        ms = (time.perf_counter() - start) * 1000
        with self._lock:
            self.events.append((time.time(), status, ms))

    def _loop(self):
        while not self._stop.is_set():
            t0 = time.perf_counter()
            self._one()
            self._stop.wait(max(0.0, self.interval - (time.perf_counter() - t0)))

    def start(self):
        for _ in range(self.workers):
            t = threading.Thread(target=self._loop, daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def stop(self):
        self._stop.set()
        for t in self._threads:
            t.join(timeout=3)

    def snapshot(self, since=0.0, until=float("inf")):
        with self._lock:
            ev = [e for e in self.events if since <= e[0] <= until]
        total = len(ev)
        failed = sum(1 for e in ev if e[1] == 0 or e[1] >= 500)
        return {"requests": total, "failed": failed,
                "error_rate_pct": round(100.0 * failed / total, 1) if total else 0.0}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8099")
    ap.add_argument("--rps", type=float, default=20)
    ap.add_argument("--duration", type=float, default=60)
    a = ap.parse_args()
    lg = LoadGen(a.url, a.rps).start()
    try:
        time.sleep(a.duration)
    except KeyboardInterrupt:
        pass
    lg.stop()
    print(json.dumps(lg.snapshot()))
