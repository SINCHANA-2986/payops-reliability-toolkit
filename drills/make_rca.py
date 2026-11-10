#!/usr/bin/env python3
"""Generate RCA reports and a results summary from drills/results/results.json.

Every number in the generated documents comes from the measured drill results. The
"root cause" of each incident is the fault we injected on purpose; the value of the
RCA is the detection analysis and the prevention actions.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
rows = {r["drill"]: r for r in json.loads((ROOT / "drills/results/results.json").read_text())}
OUT = ROOT / "docs" / "rca"
OUT.mkdir(parents=True, exist_ok=True)

TEXT = {
    "redis_outage": {
        "id": "RCA-001", "title": "Redis dependency outage", "sev": "SEV-1 (payments failing closed)",
        "cause": "The Redis process was killed (SIGKILL), simulating a crash or node loss. The API stores rate-limit, "
                 "velocity and idempotency state in Redis and deliberately fails closed, so every authorization "
                 "returned HTTP 503 while Redis was down.",
        "fix": "Restarted Redis (runbook: Service down, step 3). It reloaded its last RDB snapshot from the data directory "
               "and readiness recovered.",
        "prevent": [
            "Run Redis under a supervisor with automatic restart (done in docker-compose via `restart: unless-stopped`).",
            "Alert `PayOpsRedisDown` fires after 10 s of `payops_redis_up == 0` (monitoring/alert_rules.yml).",
            "Action item (not done): add a Redis replica with Sentinel or managed Redis so a single process crash does not cause an outage.",
            "Action item (not done): short client-side retry with backoff for transient Redis connection errors.",
        ],
    },
    "port_blocked": {
        "id": "RCA-002", "title": "API port blocked by a firewall rule", "sev": "SEV-1 (service unreachable)",
        "cause": "An iptables rule dropped TCP/8099, simulating a bad firewall change. The process was healthy, but "
                 "clients got no response and timed out.",
        "fix": "Listed rules with `iptables -S INPUT`, identified the DROP rule for port 8099 and deleted it (runbook: Service down, step 2).",
        "prevent": [
            "Alert `PayOpsServiceDown` (black-box readiness probe) catches reachability problems that process-level metrics miss.",
            "Finding: detection took longer here than for the other drills because the checker runs its checks one after another "
            "and each timed-out TCP and HTTP check waits for its 1 s timeout. Action item (not done): run checks in parallel and lower timeouts.",
            "Require peer review and a rollback plan for firewall changes.",
        ],
    },
    "disk_full": {
        "id": "RCA-003", "title": "Data filesystem nearly full", "sev": "SEV-3 (no user impact yet)",
        "cause": "A runaway file filled about 95% of the small data filesystem, simulating unrotated logs or dumps. "
                 "Redis snapshots need free space, so a full disk would eventually break persistence and backups.",
        "fix": "Found the largest file with `du -ah | sort -rh | head` and removed it (runbook: Disk full).",
        "prevent": [
            "Disk threshold in `scripts/healthcheck.py` (warn 80%, fail 90%) and alert `HostDiskAlmostFull` (<15% free) in Prometheus.",
            "Run `scripts/maintenance.sh` from cron to compress and delete old logs.",
            "Action item (not done): alert on predicted time-to-full (`predict_linear`) to get warning earlier.",
        ],
    },
    "high_latency": {
        "id": "RCA-004", "title": "Downstream latency degradation", "sev": "SEV-2 (latency SLO breached, no errors)",
        "cause": "The simulated downstream risk scorer was slowed by 800 ms. Requests still succeeded (HTTP 200) but were "
                 "far slower than the 300 ms latency target.",
        "fix": "Disabled the slow path through the fault-injection endpoint (stand-in for rolling back the bad config or "
               "failing over; runbook: High latency).",
        "prevent": [
            "Finding: error-rate and availability alerts stay silent in this failure because nothing returns an error. "
            "Detection came from the synthetic canary transaction and the latency alert `PayOpsHighLatencyP95`.",
            "The 1 s downstream timeout bounds worst-case latency; this injected delay stayed under it, so no 504s occurred.",
            "Action item (not done): circuit breaker around the downstream call so sustained slowness degrades gracefully.",
        ],
    },
    "bad_deploy": {
        "id": "RCA-005", "title": "Bad release rolled back automatically", "sev": "SEV-2 (brief outage during deploy)",
        "cause": "Release `v2-bad` was deployed with a failing readiness endpoint, simulating a defective build. "
                 "`scripts/deploy.sh` stopped the running release, started the new one in place, saw the readiness "
                 "check fail, and rolled back to the previous release.",
        "fix": "Automatic: the deploy script's post-deploy health check detected the failure and restored `v1`; "
               "the running version was confirmed afterwards.",
        "prevent": [
            "Finding: rollback worked, but because the new release replaces the old one in place, users saw an outage for "
            "the whole health-check window plus the restart of the old release. Action item (not done): start the new "
            "release on a second port and switch traffic only after it is healthy (blue/green).",
            "Action item (not done): shorten the readiness wait, and add a pre-deploy smoke test in CI so a broken build never reaches deploy.",
            "Keep the post-deploy health check and automatic rollback (already in place).",
        ],
    },
}


def fmt(v, unit=""):
    return "n/a" if v is None else f"{v}{unit}"


for name, t in TEXT.items():
    r = rows.get(name)
    if not r or r.get("status") != "OK":
        continue
    extra = ""
    if name == "bad_deploy":
        extra = (f"\n- Deploy script exit code: {r.get('deploy_exit_code')} (1 = deploy failed, previous release restored)"
                 f"\n- Rolled back to previous release: {r.get('rolled_back')}"
                 f"\n- Version running after incident: {r.get('running_version_after')}")
    prevent = "\n".join(f"- {p}" for p in t["prevent"])
    (OUT / f"{t['id']}-{name.replace('_', '-')}.md").write_text(f"""# {t['id']}: {t['title']}

**Severity:** {t['sev']}
**Type:** Failure drill (fault injected on purpose in a simulated service). Measurements below come from
`drills/results/results.json` for the last drill run.

## Summary
{r['description']}.

## Impact
- Failed requests seen by the traffic generator during the incident: {r['failed_req']} of {r['total_req']} ({r['error_rate_pct']}%)
- Expected behaviour: {r['impact_note']}{extra}

## Timeline (seconds from fault injection)
| Event | Time |
|---|---|
| Fault injected | 0.00 s |
| First failing check (detected by: {', '.join(r.get('detected_by', []))}) | {fmt(r.get('detect_s'), ' s')} |
| Service healthy again, 2 consecutive OK checks (fix applied by the drill script, or by the automatic rollback for the bad deploy) | {fmt(r.get('detect_s') + r.get('recover_s') if r.get('detect_s') is not None and r.get('recover_s') is not None else None, ' s')} (recovery took {fmt(r.get('recover_s'), ' s')} after detection) |

Final state after the drill: {r.get('final_state')}.

## Root cause
{t['cause']}

## Resolution
{t['fix']}

## Detection analysis
Detection time here is how fast the automated checker saw the fault (`scripts/healthcheck.py`, polling about every 0.2 s).
The fix was applied by the drill script immediately after detection, so recovery time shows how quickly the stack comes back
once someone acts. A person following the runbook would take longer.

## Prevention and follow-up
{prevent}
""")

# ---- results summary + trends ----
lines = ["# Drill results and incident trends", "",
         "Measured by `python drills/run_drill.py all` on one host (real Redis, real API process, real iptables rule and a "
         "64 MB tmpfs). Re-run to refresh; then `python drills/make_rca.py` regenerates this page and the RCA reports.", "",
         "| Drill | Detected by | Time to detect | Time to recover after detection | Failed requests | Final state |",
         "|---|---|---|---|---|---|"]
for n, r in rows.items():
    if r.get("status") != "OK":
        lines.append(f"| {n} | {r.get('status')} | | | | |")
        continue
    lines.append(f"| {n} | {', '.join(r.get('detected_by', []))} | {fmt(r.get('detect_s'), ' s')} | {fmt(r.get('recover_s'), ' s')} | "
                 f"{r['failed_req']}/{r['total_req']} ({r['error_rate_pct']}%) | {r.get('final_state')} |")
lines += ["", "## Trends across the five incidents",
          "- Hard failures (Redis, blocked port, bad deploy) were detected quickly by several checks at once. "
          "The slowest to detect (blocked port) was limited by the checker's own sequential timeouts, not by the fault.",
          "- Two incidents caused no errors at all (disk full, high latency). Error-rate alerting alone would have missed them; "
          "they were caught by the disk check and the synthetic canary transaction.",
          "- The longest user-visible outage came from the bad deploy, even though automatic rollback worked, because "
          "releases replace the running one in place.",
          "- Recovery times for the scripted fixes are short because the script reacts instantly. Treat them as a lower bound.",
          ""]
(ROOT / "docs" / "RESULTS.md").write_text("\n".join(lines))
print("generated", len(list(OUT.glob('*.md'))), "RCA reports and docs/RESULTS.md")
