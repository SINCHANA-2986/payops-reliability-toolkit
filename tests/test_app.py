"""Tests run against a real Redis server (redis-server must be installed)."""
import socket
import subprocess
import time

import pytest
from fastapi.testclient import TestClient


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def redis_port():
    port = _free_port()
    proc = subprocess.Popen(["redis-server", "--port", str(port), "--save", "", "--appendonly", "no"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield port
    proc.terminate()
    proc.wait()


@pytest.fixture()
def client(redis_port, monkeypatch):
    monkeypatch.setenv("REDIS_URL", f"redis://127.0.0.1:{redis_port}/0")
    monkeypatch.setenv("ENABLE_FAULT_INJECTION", "1")
    from tests.conftest import reload_app
    m = reload_app()
    with TestClient(m.app) as c:
        yield c, m


def test_health_and_ready(client):
    c, _ = client
    assert c.get("/health").json()["status"] == "ok"
    assert c.get("/ready").status_code == 200


def test_approve_and_decline_limit(client):
    c, _ = client
    ok = c.post("/authorize", json={"card_token": "c1", "amount": 25, "merchant_id": "m1"})
    assert ok.status_code == 200 and ok.json()["decision"] == "approved"
    big = c.post("/authorize", json={"card_token": "c2", "amount": 9000, "merchant_id": "m1"})
    assert big.json()["decision"] == "declined_limit"


def test_velocity_decline(client):
    c, _ = client
    decisions = [c.post("/authorize", json={"card_token": "velo", "amount": 5, "merchant_id": "m2"}).json()["decision"]
                 for _ in range(12)]
    assert "declined_velocity" in decisions


def test_idempotent_replay(client):
    c, _ = client
    body = {"card_token": "c3", "amount": 10, "merchant_id": "m3", "idempotency_key": "abc-123"}
    first = c.post("/authorize", json=body).json()
    second = c.post("/authorize", json=body).json()
    assert second["idempotent_replay"] is True and second["decision"] == first["decision"]


def test_rate_limit(client):
    c, m = client
    m.RATE_LIMIT_PER_MIN = 3
    codes = [c.post("/authorize", json={"card_token": f"r{i}", "amount": 1, "merchant_id": "rl-m"}).status_code
             for i in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3:] == [429, 429]


def test_downstream_timeout_returns_504(client):
    c, m = client
    m.DOWNSTREAM_TIMEOUT_S = 0.1
    c.post("/admin/fault", json={"downstream_delay_ms": 500})
    r = c.post("/authorize", json={"card_token": "t1", "amount": 1, "merchant_id": "m4"})
    assert r.status_code == 504
    c.post("/admin/fault", json={"downstream_delay_ms": 0})


def test_metrics_exposed(client):
    c, _ = client
    c.post("/authorize", json={"card_token": "mx", "amount": 1, "merchant_id": "m5"})
    text = c.get("/metrics").text
    assert "payops_requests_total" in text and "payops_request_duration_seconds_bucket" in text
    assert "payops_redis_up" in text


def test_fails_closed_when_redis_down(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")  # nothing listens here
    from tests.conftest import reload_app
    m = reload_app()
    with TestClient(m.app) as c:
        r = c.post("/authorize", json={"card_token": "x", "amount": 1, "merchant_id": "m"})
        assert r.status_code == 503 and r.json()["error"] == "dependency_unavailable"
        assert c.get("/ready").status_code == 503


def test_break_ready_simulates_bad_release(redis_port, monkeypatch):
    monkeypatch.setenv("REDIS_URL", f"redis://127.0.0.1:{redis_port}/0")
    monkeypatch.setenv("BREAK_READY", "1")
    from tests.conftest import reload_app
    m = reload_app()
    with TestClient(m.app) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/ready").status_code == 503


def test_dashboard_and_api_endpoints(client):
    c, _ = client
    page = c.get("/")
    assert page.status_code == 200 and "PayOps control room" in page.text
    assert c.get("/static/app.js").status_code == 200
    st = c.get("/api/status").json()
    assert st["redis_up"] is True and st["ready"] is True and st["fault_injection_enabled"] is True
    assert isinstance(c.get("/api/drills").json(), list)


def test_status_reports_redis_down(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    from tests.conftest import reload_app
    m = reload_app()
    with TestClient(m.app) as c:
        st = c.get("/api/status").json()
        assert st["redis_up"] is False and st["ready"] is False
