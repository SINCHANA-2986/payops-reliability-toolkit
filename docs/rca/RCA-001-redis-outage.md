# RCA-001: Redis dependency outage

**Severity:** SEV-1 (payments failing closed)
**Type:** Failure drill (fault injected on purpose in a simulated service). Measurements below come from
`drills/results/results.json` for the last drill run.

## Summary
Redis process killed (dependency outage).

## Impact
- Failed requests seen by the traffic generator during the incident: 5 of 8 (62.5%)
- Expected behaviour: API fails closed with HTTP 503

## Timeline (seconds from fault injection)
| Event | Time |
|---|---|
| Fault injected | 0.00 s |
| First failing check (detected by: http_ready, tcp_redis, canary_authorize) | 0.06 s |
| Service healthy again, 2 consecutive OK checks (fix applied by the drill script, or by the automatic rollback for the bad deploy) | 0.28 s (recovery took 0.22 s after detection) |

Final state after the drill: OK.

## Root cause
The Redis process was killed (SIGKILL), simulating a crash or node loss. The API stores rate-limit, velocity and idempotency state in Redis and deliberately fails closed, so every authorization returned HTTP 503 while Redis was down.

## Resolution
Restarted Redis (runbook: Service down, step 3). It reloaded its last RDB snapshot from the data directory and readiness recovered.

## Detection analysis
Detection time here is how fast the automated checker saw the fault (`scripts/healthcheck.py`, polling about every 0.2 s).
The fix was applied by the drill script immediately after detection, so recovery time shows how quickly the stack comes back
once someone acts. A person following the runbook would take longer.

## Prevention and follow-up
- Run Redis under a supervisor with automatic restart (done in docker-compose via `restart: unless-stopped`).
- Alert `PayOpsRedisDown` fires after 10 s of `payops_redis_up == 0` (monitoring/alert_rules.yml).
- Action item (not done): add a Redis replica with Sentinel or managed Redis so a single process crash does not cause an outage.
- Action item (not done): short client-side retry with backoff for transient Redis connection errors.
