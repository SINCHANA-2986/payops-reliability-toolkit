# RCA-003: Data filesystem nearly full

**Severity:** SEV-3 (no user impact yet)
**Type:** Failure drill (fault injected on purpose in a simulated service). Measurements below come from
`drills/results/results.json` for the last drill run.

## Summary
Data filesystem filled to ~95% of 64 MB.

## Impact
- Failed requests seen by the traffic generator during the incident: 0 of 8 (0.0%)
- Expected behaviour: Service still answers; risk is Redis snapshot failures

## Timeline (seconds from fault injection)
| Event | Time |
|---|---|
| Fault injected | 0.00 s |
| First failing check (detected by: disk:/tmp/payops-drill/data) | 0.09 s |
| Service healthy again, 2 consecutive OK checks (fix applied by the drill script, or by the automatic rollback for the bad deploy) | 0.3 s (recovery took 0.21 s after detection) |

Final state after the drill: OK.

## Root cause
A runaway file filled about 95% of the small data filesystem, simulating unrotated logs or dumps. Redis snapshots need free space, so a full disk would eventually break persistence and backups.

## Resolution
Found the largest file with `du -ah | sort -rh | head` and removed it (runbook: Disk full).

## Detection analysis
Detection time here is how fast the automated checker saw the fault (`scripts/healthcheck.py`, polling about every 0.2 s).
The fix was applied by the drill script immediately after detection, so recovery time shows how quickly the stack comes back
once someone acts. A person following the runbook would take longer.

## Prevention and follow-up
- Disk threshold in `scripts/healthcheck.py` (warn 80%, fail 90%) and alert `HostDiskAlmostFull` (<15% free) in Prometheus.
- Run `scripts/maintenance.sh` from cron to compress and delete old logs.
- Action item (not done): alert on predicted time-to-full (`predict_linear`) to get warning earlier.
