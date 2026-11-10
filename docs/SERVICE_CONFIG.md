# Service Configuration Reference

## Components and ports
| Component | Port | Purpose |
|---|---|---|
| payops-api (FastAPI/uvicorn) | 8099 | `/authorize`, `/health`, `/ready`, `/metrics` |
| Redis | 6379 (6390 in local drills) | rate limits, card velocity, idempotency keys |
| Prometheus | 9090 | metrics and alert evaluation |
| Alertmanager | 9093 | alert routing to the webhook sink |
| Grafana | 3000 | dashboard "PayOps Reliability" |
| node-exporter | 9100 | host CPU, memory, disk |
| blackbox-exporter | 9115 | HTTP readiness probe |

## API environment variables
| Variable | Default | Meaning |
|---|---|---|
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | Redis connection |
| `APP_VERSION` | `dev` | release tag shown in `/health` and `payops_build_info` |
| `BREAK_READY` | `0` | `1` makes `/ready` fail (simulates a bad release) |
| `RATE_LIMIT_PER_MIN` | `100000` | per-merchant requests per minute before HTTP 429 |
| `DOWNSTREAM_TIMEOUT_S` | `1.0` | timeout for the risk scorer (returns 504) |
| `ENABLE_FAULT_INJECTION` | `0` | enables `POST /admin/fault`; drills only |

## Endpoints
| Path | Purpose |
|---|---|
| `GET /` | live control-room dashboard |
| `POST /authorize` | simulated authorization |
| `GET /health`, `GET /ready`, `GET /metrics` | liveness, readiness, Prometheus metrics |
| `GET /api/status` | release, Redis state, readiness, uptime, fault-injection state (used by the dashboard) |
| `GET /api/drills` | measured drill results (`DRILLS_RESULTS_PATH`, default `drills/results/results.json`) |
| `POST /admin/fault` | inject downstream delay or failure; needs `ENABLE_FAULT_INJECTION=1` |

## Behaviour to know
- Decisions: `approved`, `declined_limit` (amount above 5000), `declined_velocity` (more than 10 per card per minute).
- Redis errors return HTTP 503 `dependency_unavailable`: the service fails closed.
- Downstream errors return 502, timeouts 504.

## Checker thresholds (`scripts/healthcheck.py` defaults)
Canary latency warn 300 ms; CPU warn 85%; memory warn 90%; disk warn 80% / fail 90%; per-check timeout 1 s.
Exit codes: 0 OK, 1 WARN, 2 FAIL.

## Files and directories
`runtime/releases/<tag>` release copies, `runtime/current` symlink to the live release, `runtime/logs` app logs,
`backups/` Redis RDB backups with `.sha256` and `.meta`, `drills/results/` measured drill output.
