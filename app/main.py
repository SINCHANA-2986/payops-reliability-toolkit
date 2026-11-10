"""PayOps API: a simulated payment-authorization service used as the system under watch.

This is a SIMULATION for operations practice. It does not process real payments.
It depends on Redis (rate limiting, idempotency, velocity checks) and a simulated
downstream risk scorer, so there are real dependencies that can fail.
"""
import asyncio
import json
import os
import random
import time
from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as aioredis
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel, Field

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
APP_VERSION = os.getenv("APP_VERSION", "dev")
BREAK_READY = os.getenv("BREAK_READY", "0") == "1"  # simulates a bad release
RATE_LIMIT_PER_MIN = int(os.getenv("RATE_LIMIT_PER_MIN", "100000"))
ENABLE_FAULT_INJECTION = os.getenv("ENABLE_FAULT_INJECTION", "0") == "1"
DOWNSTREAM_TIMEOUT_S = float(os.getenv("DOWNSTREAM_TIMEOUT_S", "1.0"))
DECLINE_ABOVE = 5000.0
VELOCITY_LIMIT = 10  # max authorizations per card per minute

REQUESTS = Counter("payops_requests_total", "HTTP requests", ["endpoint", "status"])
LATENCY = Histogram(
    "payops_request_duration_seconds",
    "Request latency in seconds",
    ["endpoint"],
    buckets=(0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1, 2, 5),
)
DECISIONS = Counter("payops_authorizations_total", "Authorization decisions", ["decision"])
RATE_LIMITED = Counter("payops_rate_limited_total", "Requests rejected by the rate limiter")
DOWNSTREAM_ERRORS = Counter("payops_downstream_errors_total", "Downstream risk scorer failures", ["kind"])
REDIS_UP = Gauge("payops_redis_up", "1 if Redis answered the last ping, else 0")
BUILD_INFO = Gauge("payops_build_info", "Running release", ["version"])

faults = {"downstream_delay_ms": 0, "downstream_fail": False}
redis_client: aioredis.Redis | None = None


async def _ping_loop():
    while True:
        try:
            await redis_client.ping()
            REDIS_UP.set(1)
        except Exception:
            REDIS_UP.set(0)
        await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global redis_client
    redis_client = aioredis.from_url(
        REDIS_URL, socket_timeout=0.5, socket_connect_timeout=0.5, decode_responses=True
    )
    BUILD_INFO.labels(version=APP_VERSION).set(1)
    REDIS_UP.set(0)
    task = asyncio.create_task(_ping_loop())
    yield
    task.cancel()
    await redis_client.aclose()


app = FastAPI(title="PayOps API (simulation)", version=APP_VERSION, lifespan=lifespan)

STATIC_DIR = Path(__file__).resolve().parent / "static"
START_TIME = time.time()
DRILLS_RESULTS_PATH = Path(os.getenv(
    "DRILLS_RESULTS_PATH", Path(__file__).resolve().parents[1] / "drills" / "results" / "results.json"))
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class AuthRequest(BaseModel):
    card_token: str = Field(min_length=1)
    amount: float = Field(gt=0)
    merchant_id: str = Field(min_length=1)
    idempotency_key: str | None = None


@app.middleware("http")
async def observe(request: Request, call_next):
    endpoint = request.url.path
    start = time.perf_counter()
    response = await call_next(request)
    if endpoint != "/metrics":
        LATENCY.labels(endpoint).observe(time.perf_counter() - start)
        REQUESTS.labels(endpoint, str(response.status_code)).inc()
    return response


@app.get("/health")
async def health():
    """Liveness: the process is up."""
    return {"status": "ok", "version": APP_VERSION}


@app.get("/ready")
async def ready():
    """Readiness: the service can do useful work (Redis reachable)."""
    if BREAK_READY:
        return JSONResponse({"status": "not_ready", "reason": "release_fault"}, status_code=503)
    try:
        await redis_client.ping()
        REDIS_UP.set(1)
        return {"status": "ready", "version": APP_VERSION}
    except Exception:
        REDIS_UP.set(0)
        return JSONResponse({"status": "not_ready", "reason": "redis_unreachable"}, status_code=503)


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


async def _risk_score(amount: float) -> float:
    """Simulated downstream risk scorer; latency and failures can be injected."""
    delay = random.uniform(0.005, 0.02) + faults["downstream_delay_ms"] / 1000.0
    await asyncio.sleep(delay)
    if faults["downstream_fail"]:
        raise RuntimeError("risk scorer unavailable")
    return min(amount / 10000.0, 1.0)


@app.post("/authorize")
async def authorize(req: AuthRequest):
    minute = int(time.time() // 60)
    try:
        if req.idempotency_key:
            cached = await redis_client.get(f"idem:{req.idempotency_key}")
            if cached:
                return {"decision": cached, "idempotent_replay": True}
        count = await redis_client.incr(f"rl:{req.merchant_id}:{minute}")
        if count == 1:
            await redis_client.expire(f"rl:{req.merchant_id}:{minute}", 60)
        if count > RATE_LIMIT_PER_MIN:
            RATE_LIMITED.inc()
            return JSONResponse(
                {"error": "rate_limited"}, status_code=429, headers={"Retry-After": "60"}
            )
        velocity = await redis_client.incr(f"vel:{req.card_token}:{minute}")
        if velocity == 1:
            await redis_client.expire(f"vel:{req.card_token}:{minute}", 60)
    except Exception:
        REDIS_UP.set(0)
        # Payments fail closed: if safety state is unavailable we do not authorize.
        DECISIONS.labels("error").inc()
        return JSONResponse({"error": "dependency_unavailable"}, status_code=503)

    try:
        risk = await asyncio.wait_for(_risk_score(req.amount), timeout=DOWNSTREAM_TIMEOUT_S)
    except asyncio.TimeoutError:
        DOWNSTREAM_ERRORS.labels("timeout").inc()
        DECISIONS.labels("error").inc()
        return JSONResponse({"error": "downstream_timeout"}, status_code=504)
    except Exception:
        DOWNSTREAM_ERRORS.labels("error").inc()
        DECISIONS.labels("error").inc()
        return JSONResponse({"error": "downstream_error"}, status_code=502)

    if req.amount > DECLINE_ABOVE:
        decision = "declined_limit"
    elif velocity > VELOCITY_LIMIT:
        decision = "declined_velocity"
    else:
        decision = "approved"
    DECISIONS.labels(decision).inc()
    if req.idempotency_key:
        try:
            await redis_client.set(f"idem:{req.idempotency_key}", decision, ex=300)
        except Exception:
            pass
    return {"decision": decision, "risk": round(risk, 3), "version": APP_VERSION}


class FaultRequest(BaseModel):
    downstream_delay_ms: int = 0
    downstream_fail: bool = False


@app.post("/admin/fault")
async def set_fault(req: FaultRequest):
    """Fault injection for failure drills. Disabled unless ENABLE_FAULT_INJECTION=1."""
    if not ENABLE_FAULT_INJECTION:
        return JSONResponse({"error": "fault_injection_disabled"}, status_code=403)
    faults["downstream_delay_ms"] = req.downstream_delay_ms
    faults["downstream_fail"] = req.downstream_fail
    return faults


@app.get("/", include_in_schema=False)
async def dashboard():
    """Live control-room dashboard (static page that reads /metrics and /api/*)."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status")
async def api_status():
    """Live state for the dashboard: dependency health, release and fault-injection state."""
    try:
        await redis_client.ping()
        redis_ok = True
    except Exception:
        redis_ok = False
    REDIS_UP.set(1 if redis_ok else 0)
    return {
        "version": APP_VERSION,
        "redis_up": redis_ok,
        "ready": redis_ok and not BREAK_READY,
        "uptime_s": round(time.time() - START_TIME, 1),
        "fault_injection_enabled": ENABLE_FAULT_INJECTION,
        "faults": faults,
    }


@app.get("/api/drills")
async def api_drills():
    """Measured failure-drill results (written by drills/run_drill.py), or an empty list."""
    try:
        return json.loads(DRILLS_RESULTS_PATH.read_text())
    except (OSError, ValueError):
        return []
