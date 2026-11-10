#!/usr/bin/env python3
"""Failure-drill runner. Starts a real stack (Redis + API + traffic + watcher), injects a
fault, executes the runbook fix, and records measured detection and recovery times.

  python drills/run_drill.py all
  python drills/run_drill.py redis_outage

Measured per drill (all wall-clock, from this run):
  detect_s   inject -> first non-OK result from scripts/healthcheck.py (MTTD)
  recover_s  detect -> first of 2 consecutive OK results after the fix is applied (MTTR)
  failed_req / error_rate_pct  user-facing failures seen by the traffic generator
The "fix" step is executed by this script immediately after detection, acting as the
on-call engineer following docs/RUNBOOK.md. Real human response times will be longer.
"""
import argparse
import csv
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import healthcheck  # noqa: E402
from load_gen import LoadGen  # noqa: E402

WORK = Path(os.getenv("DRILL_WORK", "/tmp/payops-drill"))
API_PORT, REDIS_PORT = 8099, 6390
URL = f"http://127.0.0.1:{API_PORT}"
RESULTS = ROOT / "drills" / "results"
DATA_MOUNT = WORK / "data"  # small tmpfs holding Redis data, used for the disk drill
DISK_MB = 64

env_base = dict(os.environ, PORT=str(API_PORT), RUNTIME_DIR=str(WORK / "runtime"),
                REDIS_URL=f"redis://127.0.0.1:{REDIS_PORT}/0", ENABLE_FAULT_INJECTION="1")


def sh(cmd, **kw):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, **kw)


class Stack:
    def __init__(self):
        self.redis = None
        self.results = []  # health check results with timestamps
        self._watch_stop = threading.Event()
        self.watcher = None
        self.load = None

    # ---- lifecycle ----
    def start_redis(self):
        DATA_MOUNT.mkdir(parents=True, exist_ok=True)
        self.redis = subprocess.Popen(
            ["redis-server", "--port", str(REDIS_PORT), "--dir", str(DATA_MOUNT), "--dbfilename", "dump.rdb",
             "--save", "1 1", "--appendonly", "no"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            if sh(f"redis-cli -p {REDIS_PORT} ping").stdout.strip() == "PONG":
                return
            time.sleep(0.1)
        raise RuntimeError("redis failed to start")

    def stop_redis(self, sig=signal.SIGKILL):
        if self.redis and self.redis.poll() is None:
            self.redis.send_signal(sig)
            self.redis.wait()

    def deploy(self, tag, extra=""):
        self.last_deploy = sh(f"{ROOT}/scripts/deploy.sh {tag} {extra}", env=env_base)
        return self.last_deploy

    def start(self):
        shutil.rmtree(WORK, ignore_errors=True)
        WORK.mkdir(parents=True)
        # Small dedicated filesystem so the disk-full drill cannot harm the host.
        DATA_MOUNT.mkdir(parents=True)
        r = sh(f"mount -t tmpfs -o size={DISK_MB}m tmpfs {DATA_MOUNT}")
        self.has_tmpfs = r.returncode == 0
        self.start_redis()
        d = self.deploy("v1")
        if d.returncode != 0:
            raise RuntimeError("initial deploy failed: " + d.stdout + d.stderr)
        self.load = LoadGen(URL, rps=20).start()
        args = healthcheck.parse_args(["--url", URL, "--redis-port", str(REDIS_PORT),
                                       "--disk-path", str(DATA_MOUNT if self.has_tmpfs else "/")])
        self.hc_args = args
        self.watcher = threading.Thread(target=self._watch, daemon=True)
        self.watcher.start()
        time.sleep(3)  # let a baseline accumulate

    def _watch(self):
        while not self._watch_stop.is_set():
            res = healthcheck.run_checks(self.hc_args)
            self.results.append(res)
            time.sleep(0.2)

    def stop(self):
        self._watch_stop.set()
        if self.load:
            self.load.stop()
        sh(f"kill $(cat {WORK}/runtime/app.pid) 2>/dev/null")
        self.stop_redis()
        sh(f"umount {DATA_MOUNT} 2>/dev/null")

    # ---- measurement helpers ----
    def wait_detect(self, t_inject, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for r in list(self.results):
                if r["ts"] >= t_inject and r["overall"] != "OK":
                    bad = [c["check"] for c in r["checks"] if c["status"] != "OK"]
                    return r["ts"], bad
            time.sleep(0.05)
        return None, []

    def wait_recover(self, t_after, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            after = [r for r in list(self.results) if r["ts"] >= t_after]
            for a, b in zip(after, after[1:]):
                if a["overall"] == "OK" and b["overall"] == "OK":
                    return a["ts"]
            time.sleep(0.05)
        return None


def post_fault(delay_ms=0, fail=False):
    body = json.dumps({"downstream_delay_ms": delay_ms, "downstream_fail": fail}).encode()
    req = urllib.request.Request(URL + "/admin/fault", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=2).read()


# ---------------- drills ----------------
# Each drill returns (inject_fn, fix_fn, description, impact_note)

def d_redis_outage(s):
    def inject():
        s.stop_redis(signal.SIGKILL)

    def fix():
        s.start_redis()  # runbook: restart Redis, data reloaded from the last RDB snapshot
    return inject, fix, "Redis process killed (dependency outage)", "API fails closed with HTTP 503"


def d_port_blocked(s):
    rule = f"INPUT -p tcp --dport {API_PORT} -j DROP"

    def inject():
        sh(f"iptables -I {rule}")

    def fix():
        sh(f"iptables -D {rule}")
    return inject, fix, f"Firewall rule drops TCP/{API_PORT} (network path blocked)", "Clients time out; no TCP response"


def d_disk_full(s):
    fill = DATA_MOUNT / "runaway.bin"

    def inject():
        # fill ~95% of the small data filesystem (e.g. a runaway log/dump)
        sh(f"fallocate -l {int(DISK_MB * 0.95)}M {fill}")

    def fix():
        fill.unlink(missing_ok=True)  # runbook: find the largest file with du, remove/rotate it
    return inject, fix, f"Data filesystem filled to ~95% of {DISK_MB} MB", "Service still answers; risk is Redis snapshot failures"


def d_high_latency(s):
    def inject():
        post_fault(delay_ms=800)

    def fix():
        post_fault(delay_ms=0)  # runbook: disable slow downstream path / roll back config
    return inject, fix, "Downstream risk scorer slowed by 800 ms", "Requests succeed but exceed latency SLO"


def d_bad_deploy(s):
    state = {}

    def inject():
        # deploy a broken release; deploy.sh runs its own health check and rolls back
        state["res"] = s.deploy("v2-bad", "--break-ready")

    def fix():
        pass  # rollback is automatic inside deploy.sh
    return inject, fix, "Release v2-bad deployed with failing readiness (auto-rollback expected)", \
        "Brief outage while the bad release starts and is rolled back"


DRILLS = {
    "redis_outage": d_redis_outage,
    "port_blocked": d_port_blocked,
    "disk_full": d_disk_full,
    "high_latency": d_high_latency,
    "bad_deploy": d_bad_deploy,
}


def run_one(name):
    s = Stack()
    out = {"drill": name}
    try:
        s.start()
        if name == "disk_full" and not s.has_tmpfs:
            return {**out, "status": "SKIPPED", "note": "could not mount tmpfs on this host"}
        inject, fix, desc, note = DRILLS[name](s)
        out.update(description=desc, impact_note=note)
        t_inject = time.time()
        inject()
        t_inject_done = time.time()
        t_detect, bad = s.wait_detect(t_inject)
        out["status"] = "OK"
        if name == "bad_deploy":
            # detection = first unhealthy result; recovery = healthy again after auto-rollback
            time.sleep(0.5)
            t_recover = s.wait_recover(t_inject_done)
            out["deploy_exit_code"] = s.last_deploy.returncode  # 1 = failed deploy, previous release restored
            out["rolled_back"] = "rollback to v1 complete" in s.last_deploy.stdout
            with urllib.request.urlopen(URL + "/health", timeout=2) as r:
                out["running_version_after"] = json.loads(r.read())["version"]
        else:
            if t_detect is None:
                return {**out, "status": "NOT_DETECTED"}
            t_fix = time.time()
            fix()
            t_recover = s.wait_recover(t_fix)
        if t_detect is None:
            out.update(detect_s=None, detected_by=[], recover_s=None, status="NOT_DETECTED")
            return out
        out["detect_s"] = round(t_detect - t_inject, 2)
        out["detected_by"] = bad
        if t_recover is None:
            out.update(recover_s=None, status="NOT_RECOVERED")
        else:
            out["recover_s"] = round(t_recover - t_detect, 2)
        time.sleep(1.5)  # let traffic settle so tally covers the whole incident
        end = t_recover if t_recover else time.time()
        snap = s.load.snapshot(since=t_inject, until=end)
        out.update(failed_req=snap["failed"], total_req=snap["requests"], error_rate_pct=snap["error_rate_pct"])
        # Self-check: stack must be fully healthy again at the end
        time.sleep(0.6)
        out["final_state"] = s.results[-1]["overall"]
        return out
    finally:
        s.stop()
        sh(f"iptables -D INPUT -p tcp --dport {API_PORT} -j DROP 2>/dev/null")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("drill", choices=list(DRILLS) + ["all"])
    a = ap.parse_args()
    names = list(DRILLS) if a.drill == "all" else [a.drill]
    RESULTS.mkdir(parents=True, exist_ok=True)
    rows = []
    for n in names:
        print(f"== drill: {n}", flush=True)
        r = run_one(n)
        print(json.dumps(r), flush=True)
        rows.append(r)
    (RESULTS / "results.json").write_text(json.dumps(rows, indent=2))
    fields = ["drill", "status", "description", "detect_s", "detected_by", "recover_s",
              "failed_req", "total_req", "error_rate_pct", "final_state"]
    with open(RESULTS / "drills.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            r = dict(r)
            r["detected_by"] = ";".join(r.get("detected_by", []) or [])
            w.writerow(r)
    print(f"wrote {RESULTS/'drills.csv'}")


if __name__ == "__main__":
    main()
