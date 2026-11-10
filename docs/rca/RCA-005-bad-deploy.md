# RCA-005: Bad release rolled back automatically

**Severity:** SEV-2 (brief outage during deploy)
**Type:** Failure drill (fault injected on purpose in a simulated service). Measurements below come from
`drills/results/results.json` for the last drill run.

## Summary
Release v2-bad deployed with failing readiness (auto-rollback expected).

## Impact
- Failed requests seen by the traffic generator during the incident: 76 of 184 (41.3%)
- Expected behaviour: Brief outage while the bad release starts and is rolled back
- Deploy script exit code: 1 (1 = deploy failed, previous release restored)
- Rolled back to previous release: True
- Version running after incident: v1

## Timeline (seconds from fault injection)
| Event | Time |
|---|---|
| Fault injected | 0.00 s |
| First failing check (detected by: tcp_api, http_ready, canary_authorize) | 0.06 s |
| Service healthy again, 2 consecutive OK checks (fix applied by the drill script, or by the automatic rollback for the bad deploy) | 9.14 s (recovery took 9.08 s after detection) |

Final state after the drill: OK.

## Root cause
Release `v2-bad` was deployed with a failing readiness endpoint, simulating a defective build. `scripts/deploy.sh` stopped the running release, started the new one in place, saw the readiness check fail, and rolled back to the previous release.

## Resolution
Automatic: the deploy script's post-deploy health check detected the failure and restored `v1`; the running version was confirmed afterwards.

## Detection analysis
Detection time here is how fast the automated checker saw the fault (`scripts/healthcheck.py`, polling about every 0.2 s).
The fix was applied by the drill script immediately after detection, so recovery time shows how quickly the stack comes back
once someone acts. A person following the runbook would take longer.

## Prevention and follow-up
- Finding: rollback worked, but because the new release replaces the old one in place, users saw an outage for the whole health-check window plus the restart of the old release. Action item (not done): start the new release on a second port and switch traffic only after it is healthy (blue/green).
- Action item (not done): shorten the readiness wait, and add a pre-deploy smoke test in CI so a broken build never reaches deploy.
- Keep the post-deploy health check and automatic rollback (already in place).
