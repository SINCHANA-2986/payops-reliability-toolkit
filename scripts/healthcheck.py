#!/usr/bin/env python3
"""Health, network and host checker for the PayOps stack.

Checks: DNS resolution, TCP port reachability, HTTP readiness, a synthetic
authorization (canary) with a latency threshold, Redis TCP reachability, and host
CPU / memory / disk. Prints one JSON line per run; exit code 0 = OK, 1 = WARN,
2 = FAIL, so it can be used from cron, CI or a deploy script.

Examples:
  python scripts/healthcheck.py --url http://127.0.0.1:8099
  python scripts/healthcheck.py --url http://127.0.0.1:8099 --watch --interval 1 --log logs/health.jsonl
"""
import argparse
import json
import socket
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

try:
    import psutil
except ImportError:  # host checks are skipped if psutil is missing
    psutil = None

OK, WARN, FAIL = "OK", "WARN", "FAIL"
RANK = {OK: 0, WARN: 1, FAIL: 2}


def _result(name, status, detail="", value=None):
    return {"check": name, "status": status, "detail": detail, "value": value}


def check_dns(host, timeout=2.0):
    start = time.perf_counter()
    try:
        socket.setdefaulttimeout(timeout)
        socket.getaddrinfo(host, None)
        ms = round((time.perf_counter() - start) * 1000, 1)
        return _result("dns", OK, f"{host} resolved", ms)
    except OSError as exc:
        return _result("dns", FAIL, f"{host}: {exc}")


def check_tcp(name, host, port, timeout=1.0):
    start = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            ms = round((time.perf_counter() - start) * 1000, 1)
            return _result(name, OK, f"{host}:{port} reachable", ms)
    except OSError as exc:
        return _result(name, FAIL, f"{host}:{port} {type(exc).__name__}: {exc}")


def _http(method, url, body=None, timeout=1.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, (time.perf_counter() - start) * 1000
    except urllib.error.HTTPError as exc:
        return exc.code, (time.perf_counter() - start) * 1000


def check_ready(url, timeout=1.0):
    try:
        status, ms = _http("GET", url.rstrip("/") + "/ready", timeout=timeout)
    except Exception as exc:
        return _result("http_ready", FAIL, f"{type(exc).__name__}: {exc}")
    if status == 200:
        return _result("http_ready", OK, "HTTP 200", round(ms, 1))
    return _result("http_ready", FAIL, f"HTTP {status}", round(ms, 1))


def check_canary(url, latency_warn_ms, timeout=3.0):
    """Synthetic transaction: exercises API + Redis + downstream like a real request."""
    body = {"card_token": "canary-card", "amount": 1.0, "merchant_id": "canary-merchant"}
    try:
        status, ms = _http("POST", url.rstrip("/") + "/authorize", body, timeout=timeout)
    except Exception as exc:
        return _result("canary_authorize", FAIL, f"{type(exc).__name__}: {exc}")
    ms = round(ms, 1)
    if status != 200:
        return _result("canary_authorize", FAIL, f"HTTP {status}", ms)
    if ms > latency_warn_ms:
        return _result("canary_authorize", WARN, f"latency {ms} ms > {latency_warn_ms} ms", ms)
    return _result("canary_authorize", OK, "approved path healthy", ms)


def check_host(disk_paths, cpu_warn, mem_warn, disk_warn, disk_fail):
    out = []
    if psutil is None:
        return [_result("host", WARN, "psutil not installed; host checks skipped")]
    cpu = psutil.cpu_percent(interval=None)
    out.append(_result("cpu", WARN if cpu > cpu_warn else OK, f"{cpu}%", cpu))
    mem = psutil.virtual_memory().percent
    out.append(_result("memory", WARN if mem > mem_warn else OK, f"{mem}%", mem))
    for path in disk_paths:
        try:
            pct = psutil.disk_usage(path).percent
        except OSError as exc:
            out.append(_result(f"disk:{path}", FAIL, str(exc)))
            continue
        status = FAIL if pct >= disk_fail else WARN if pct >= disk_warn else OK
        out.append(_result(f"disk:{path}", status, f"{pct}% used", pct))
    return out


def run_checks(args):
    parsed = urlparse(args.url)
    host, port = parsed.hostname, parsed.port or 80
    checks = [check_dns(host), check_tcp("tcp_api", host, port), check_ready(args.url)]
    if args.redis_port:
        checks.append(check_tcp("tcp_redis", args.redis_host, args.redis_port))
    # Skip the canary only if the port is closed; otherwise a hang/slow API must show up.
    if checks[1]["status"] == OK:
        checks.append(check_canary(args.url, args.latency_warn_ms))
    else:
        checks.append(_result("canary_authorize", FAIL, "skipped: API port unreachable"))
    checks += check_host(args.disk_path, args.cpu_warn, args.mem_warn, args.disk_warn, args.disk_fail)
    overall = max((c["status"] for c in checks), key=RANK.get)
    return {"ts": round(time.time(), 3), "overall": overall, "checks": checks}


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="http://127.0.0.1:8099")
    p.add_argument("--redis-host", default="127.0.0.1")
    p.add_argument("--redis-port", type=int, default=0, help="0 disables the Redis TCP check")
    p.add_argument("--disk-path", action="append", default=None, help="repeatable; default /")
    p.add_argument("--latency-warn-ms", type=float, default=300.0)
    p.add_argument("--cpu-warn", type=float, default=85.0)
    p.add_argument("--mem-warn", type=float, default=90.0)
    p.add_argument("--disk-warn", type=float, default=80.0)
    p.add_argument("--disk-fail", type=float, default=90.0)
    p.add_argument("--watch", action="store_true", help="run continuously")
    p.add_argument("--interval", type=float, default=1.0)
    p.add_argument("--log", help="append JSON lines to this file")
    args = p.parse_args(argv)
    args.disk_path = args.disk_path or ["/"]
    return args


def main(argv=None):
    args = parse_args(argv)
    logf = open(args.log, "a", buffering=1) if args.log else None
    worst = OK
    try:
        while True:
            res = run_checks(args)
            line = json.dumps(res)
            print(line, flush=True)
            if logf:
                logf.write(line + "\n")
            worst = res["overall"]
            if not args.watch:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        if logf:
            logf.close()
    return RANK[worst]


if __name__ == "__main__":
    sys.exit(main())
