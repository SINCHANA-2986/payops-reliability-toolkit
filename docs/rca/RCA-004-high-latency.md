# RCA-004: Downstream latency degradation

**Severity:** SEV-2 (latency SLO breached, no errors)
**Type:** Failure drill (fault injected on purpose in a simulated service). Measurements below come from
`drills/results/results.json` for the last drill run.

## Summary
Downstream risk scorer slowed by 800 ms.

## Impact
- Failed requests seen by the traffic generator during the incident: 0 of 4 (0.0%)
- Expected behaviour: Requests succeed but exceed latency SLO

## Timeline (seconds from fault injection)
| Event | Time |
|---|---|
| Fault injected | 0.00 s |
| First failing check (detected by: canary_authorize) | 0.86 s |
| Service healthy again, 2 consecutive OK checks (fix applied by the drill script, or by the automatic rollback for the bad deploy) | 1.07 s (recovery took 0.21 s after detection) |

Final state after the drill: OK.

## Root cause
The simulated downstream risk scorer was slowed by 800 ms. Requests still succeeded (HTTP 200) but were far slower than the 300 ms latency target.

## Resolution
Disabled the slow path through the fault-injection endpoint (stand-in for rolling back the bad config or failing over; runbook: High latency).

## Detection analysis
Detection time here is how fast the automated checker saw the fault (`scripts/healthcheck.py`, polling about every 0.2 s).
The fix was applied by the drill script immediately after detection, so recovery time shows how quickly the stack comes back
once someone acts. A person following the runbook would take longer.

## Prevention and follow-up
- Finding: error-rate and availability alerts stay silent in this failure because nothing returns an error. Detection came from the synthetic canary transaction and the latency alert `PayOpsHighLatencyP95`.
- The 1 s downstream timeout bounds worst-case latency; this injected delay stayed under it, so no 504s occurred.
- Action item (not done): circuit breaker around the downstream call so sustained slowness degrades gracefully.
