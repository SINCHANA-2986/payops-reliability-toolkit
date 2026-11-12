# PayOps Reliability Toolkit

I'm a fresher aiming for technology operations roles, and I wanted something more realistic than a to-do app to practise on. So I built a small fake payment service and then did what an ops team does with a real one: watched it, broke it, fixed it and wrote down what happened.

The payment service is simulated. It never touches real money or real cards. It's only here to give the monitoring, scripts and incident write-ups something real to work on.

![Dashboard while Redis is down](docs/screenshots/dashboard-redis-down.png)

## What I built

**The service** (`app/`). A FastAPI app with a `POST /authorize` endpoint. Redis handles rate limiting, a per-card velocity check and idempotency keys, and a fake "risk scorer" sits behind it. If Redis goes down the service refuses requests with a 503 instead of approving blindly. If the scorer is too slow it returns a 504. It exposes `/health`, `/ready` and Prometheus-style `/metrics`.

**Scripts** (`scripts/`).
- `healthcheck.py` checks DNS, the TCP port, HTTP readiness, a test authorization (with a latency limit), Redis, and CPU, memory and disk. It exits 0, 1 or 2 for OK, WARN or FAIL, so other tools can use the result.
- `backup_redis.sh` backs up Redis with a checksum and keeps only the recent copies. `restore_test.sh` loads a backup into a scratch Redis and compares key counts, because a backup you've never restored isn't worth much.
- `maintenance.sh` cleans old logs and lists pending updates.
- `deploy.sh` ships a release, health-checks it, and rolls back on its own if the check fails.

**Failure drills** (`drills/`). Five ways to break it on purpose: Redis outage, a firewall-blocked port, a full disk, a slow dependency and a bad deploy. Each drill records how long the checker took to notice, how long recovery took, and how many requests failed. `make_rca.py` turns those results into the write-ups in `docs/rca/`.

**A dashboard** (`app/static/`, served at `/`). Plain HTML, CSS and JavaScript, no build step. It reads the service's own `/metrics` and shows a one-line status, a live strip of recent requests, latency and error numbers, an event log, and buttons to start test traffic or slow the scorer down. The figures are worked out in the browser from the metrics, nothing is hard-coded.

**Monitoring stack** (`monitoring/`, `docker-compose.yml`). Prometheus, Alertmanager, a Grafana dashboard, node-exporter and a blackbox probe, with alert rules for service down, Redis down, error rate, p95 latency, disk and CPU. See the notes below: I haven't started this one yet.

**Docs** (`docs/`). A runbook, SLOs, a config reference, five RCAs and an RCA template.

## Running it

You need Python 3.10+, `redis-server` and Node (Node is only for one dashboard test).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q
```

That runs 17 tests. The API ones talk to a real Redis, not a mock.


To see the dashboard:

```bash
redis-server --port 6379 --daemonize yes
ENABLE_FAULT_INJECTION=1 python -m uvicorn app.main:app --port 8099
```

Open http://localhost:8099 and press **Start test traffic**. Then slow the scorer down from the controls, or break Redis for real with `redis-cli shutdown nosave` and watch the page notice.

I use `python -m uvicorn` rather than plain `uvicorn` because if you have Conda installed, `uvicorn` can resolve to Conda's copy, which doesn't have the packages from your venv.

The drills need Linux and root, since they use iptables and a tmpfs mount. On a Mac, run them inside a privileged Ubuntu container:

```bash
python drills/run_drill.py all
python drills/make_rca.py    # rebuilds docs/RESULTS.md and docs/rca/ from the results
```

The full monitoring stack:

```bash
docker compose up -d --build
```

Grafana is at http://localhost:3000 (admin/admin), dashboard "PayOps Reliability". To trigger an alert, run `docker compose stop redis`.

## Results

The measured numbers for each drill are in [docs/RESULTS.md](docs/RESULTS.md) and `drills/results/drills.csv`. Every drill has its own RCA in `docs/rca/`.

## Things to know before you trust any of this

- The payment service, the traffic and the risk scorer are all simulated.
- The drill script applies the fix right after it detects the problem, so the recovery times are a best case. A person following the runbook would be slower.
- If the scorer's delay goes past the 1 second timeout, requests fail with 504 instead of just being slow. That's the intended behaviour, not a bug.
- The dashboard limits itself to 5 requests in flight, because browsers only allow about 6 connections to one host. When responses are slow it sends fewer requests instead of letting them queue and time out.
- The dashboard's numbers cover the time since the API process started. It's a quick view, not a replacement for Prometheus and Grafana.
- I haven't started the Docker Compose stack yet. The Prometheus, Alertmanager and Grafana files are syntax-checked only, so expect to fix small things when you first run it.
- Not built yet, and listed as follow-ups in the RCAs: blue/green deploys, running health checks in parallel, and a Redis replica.



