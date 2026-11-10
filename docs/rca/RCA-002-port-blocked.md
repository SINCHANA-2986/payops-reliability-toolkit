# RCA-002: API port blocked by a firewall rule

**Severity:** SEV-1 (service unreachable)
**Type:** Failure drill (fault injected on purpose in a simulated service). Measurements below come from
`drills/results/results.json` for the last drill run.

## Summary
Firewall rule drops TCP/8099 (network path blocked).

## Impact
- Failed requests seen by the traffic generator during the incident: 4 of 12 (33.3%)
- Expected behaviour: Clients time out; no TCP response

## Timeline (seconds from fault injection)
| Event | Time |
|---|---|
| Fault injected | 0.00 s |
| First failing check (detected by: tcp_api, http_ready, canary_authorize) | 2.04 s |
| Service healthy again, 2 consecutive OK checks (fix applied by the drill script, or by the automatic rollback for the bad deploy) | 2.25 s (recovery took 0.21 s after detection) |

Final state after the drill: OK.

## Root cause
An iptables rule dropped TCP/8099, simulating a bad firewall change. The process was healthy, but clients got no response and timed out.

## Resolution
Listed rules with `iptables -S INPUT`, identified the DROP rule for port 8099 and deleted it (runbook: Service down, step 2).

## Detection analysis
Detection time here is how fast the automated checker saw the fault (`scripts/healthcheck.py`, polling about every 0.2 s).
The fix was applied by the drill script immediately after detection, so recovery time shows how quickly the stack comes back
once someone acts. A person following the runbook would take longer.

## Prevention and follow-up
- Alert `PayOpsServiceDown` (black-box readiness probe) catches reachability problems that process-level metrics miss.
- Finding: detection took longer here than for the other drills because the checker runs its checks one after another and each timed-out TCP and HTTP check waits for its 1 s timeout. Action item (not done): run checks in parallel and lower timeouts.
- Require peer review and a rollback plan for firewall changes.
