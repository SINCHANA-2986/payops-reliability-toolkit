# Drill results and incident trends

Measured by `python drills/run_drill.py all` on one host (real Redis, real API process, real iptables rule and a 64 MB tmpfs). Re-run to refresh; then `python drills/make_rca.py` regenerates this page and the RCA reports.

| Drill | Detected by | Time to detect | Time to recover after detection | Failed requests | Final state |
|---|---|---|---|---|---|
| redis_outage | http_ready, tcp_redis, canary_authorize | 0.06 s | 0.22 s | 5/8 (62.5%) | OK |
| port_blocked | tcp_api, http_ready, canary_authorize | 2.04 s | 0.21 s | 4/12 (33.3%) | OK |
| disk_full | disk:/tmp/payops-drill/data | 0.09 s | 0.21 s | 0/8 (0.0%) | OK |
| high_latency | canary_authorize | 0.86 s | 0.21 s | 0/4 (0.0%) | OK |
| bad_deploy | tcp_api, http_ready, canary_authorize | 0.06 s | 9.08 s | 76/184 (41.3%) | OK |

## Trends across the five incidents
- Hard failures (Redis, blocked port, bad deploy) were detected quickly by several checks at once. The slowest to detect (blocked port) was limited by the checker's own sequential timeouts, not by the fault.
- Two incidents caused no errors at all (disk full, high latency). Error-rate alerting alone would have missed them; they were caught by the disk check and the synthetic canary transaction.
- The longest user-visible outage came from the bad deploy, even though automatic rollback worked, because releases replace the running one in place.
- Recovery times for the scripted fixes are short because the script reacts instantly. Treat them as a lower bound.
