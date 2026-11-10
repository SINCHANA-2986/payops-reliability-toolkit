# PayOps On-Call Runbook

Service: `payops-api` (simulated payment authorization) with Redis and a downstream risk scorer.
First step for any alert: run the checker and read which checks fail.

```bash
python scripts/healthcheck.py --url http://127.0.0.1:8099 --redis-port 6379 --disk-path /
```

| Failing check | Likely cause | Go to |
|---|---|---|
| `tcp_api` or `dns` | process down, port blocked, DNS problem | [Service down](#service-down) |
| `http_ready` (HTTP 503) | Redis unreachable, or a bad release | [Service down](#service-down), [Failed deploy](#failed-deploy) |
| `tcp_redis` | Redis down | [Service down](#service-down) |
| `canary_authorize` WARN (slow) | downstream or host slowness | [High latency](#high-latency) |
| `disk:*` | filesystem filling | [Disk full](#disk-full) |

## Service down
Alerts: `PayOpsServiceDown`, `PayOpsRedisDown`, `PayOpsHighErrorRate`.

1. **Is the process running and listening?** `ss -ltnp | grep 8099` and `curl -s -m 2 localhost:8099/health`.
   - Not listening: check `runtime/logs/app-<release>.log`, then redeploy the last good release with `scripts/deploy.sh <tag>`.
2. **Is the network path blocked?** If the process listens locally but remote/`tcp_api` checks time out, inspect the firewall:
   `iptables -S INPUT`. Remove an unexpected DROP rule: `iptables -D INPUT -p tcp --dport 8099 -j DROP`.
3. **Is Redis down?** `redis-cli -p 6379 ping`. If there is no answer, restart it
   (`docker compose restart redis`, or `redis-server ...`). Redis reloads its last snapshot; confirm with `redis-cli dbsize`.
   While Redis is down the API returns HTTP 503 on purpose (payments fail closed); do not bypass this.
4. **Verify:** two consecutive OK runs of `scripts/healthcheck.py`, error-rate panel back to ~0, alert resolves.
5. **Record:** timeline, root cause and prevention in `docs/rca/` using `docs/rca/TEMPLATE.md`.

## High latency
Alert: `PayOpsHighLatencyP95` (p95 above 300 ms for 30 s). Availability alerts stay quiet in this case.

1. Check the Grafana "p95 latency" and "Downstream errors" panels. A rise in `payops_downstream_errors_total{kind="timeout"}`
   means the risk scorer is slow or failing.
2. Check the host: `top`, `free -m`, `df -h` for CPU, memory or disk pressure.
3. Mitigate: roll back the most recent config or release, or fail over the slow dependency.
   (In the drill the slow path is cleared with `POST /admin/fault {"downstream_delay_ms": 0}`.)
4. Verify canary latency below 300 ms and p95 back under target for 5 minutes.

## Disk full
Alerts: `HostDiskAlmostFull`, checker `disk:*` WARN/FAIL.

1. Find the filesystem and the largest consumers: `df -h`, then `du -ah <mount> 2>/dev/null | sort -rh | head -20`.
2. Remove or rotate the culprit (old logs, temp dumps): `scripts/maintenance.sh --logs <dir>`; delete confirmed junk only.
3. If the disk holds Redis data, confirm snapshots still work: `redis-cli lastsave` and `redis-cli info persistence`.
4. Take a fresh backup and prove it restores: `scripts/backup_redis.sh` then `scripts/restore_test.sh`.

## Failed deploy
Deploys go through `scripts/deploy.sh <tag>`, which health-checks `/ready` and rolls back automatically.

1. Exit code `1` means the release failed and the previous release was restored; `3` means rollback also failed (page immediately).
2. Confirm the running version: `curl -s localhost:8099/health`.
3. Read `runtime/logs/app-<failed-tag>.log` for the cause. Do not redeploy the same tag until it is fixed.

## Routine maintenance
- Daily: `scripts/backup_redis.sh`, then weekly `scripts/restore_test.sh` (a backup that has never been restored is unproven).
- Daily: `scripts/maintenance.sh` (log cleanup, pending-update report). Apply updates in a change window with `--apply-updates`.
