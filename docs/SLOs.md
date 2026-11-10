# Service Level Objectives (targets for the simulation)

| SLI | Objective | How it is measured | Alert |
|---|---|---|---|
| Availability of `/authorize` | 99.5% of requests are not 5xx over 30 days | `payops_requests_total{endpoint="/authorize",status=~"5.."}` | `PayOpsHighErrorRate` (>5% for 30 s) |
| Latency of `/authorize` | p95 under 300 ms | `payops_request_duration_seconds` histogram | `PayOpsHighLatencyP95` (>0.3 s for 30 s) |
| Readiness | Probe succeeds | Blackbox probe on `/ready` | `PayOpsServiceDown` (15 s) |
| Dependency health | Redis reachable | `payops_redis_up` | `PayOpsRedisDown` (10 s) |

Error budget at 99.5% over 30 days is about 3.6 hours of full downtime. These targets are examples chosen for
the simulation, not Amex figures.

The 4xx responses (`429`, validation errors) are not counted as failures; declines are valid business outcomes.
