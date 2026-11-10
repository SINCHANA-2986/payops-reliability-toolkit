import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from scripts import healthcheck as hc


class _Handler(BaseHTTPRequestHandler):
    ready_code = 200

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(type(self).ready_code)
        self.end_headers()

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.send_response(200)
        self.end_headers()


@pytest.fixture()
def server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()


def _args(url, **extra):
    argv = ["--url", url]
    for k, v in extra.items():
        argv += [f"--{k.replace('_', '-')}", str(v)]
    return hc.parse_args(argv)


def test_ok_when_ready(server):
    _Handler.ready_code = 200
    url = f"http://127.0.0.1:{server.server_port}"
    res = hc.run_checks(_args(url, disk_fail=100, disk_warn=100, cpu_warn=1000, mem_warn=1000))
    statuses = {c["check"]: c["status"] for c in res["checks"]}
    assert statuses["tcp_api"] == "OK" and statuses["http_ready"] == "OK"
    assert res["overall"] == "OK"


def test_fail_when_not_ready(server):
    _Handler.ready_code = 503
    url = f"http://127.0.0.1:{server.server_port}"
    res = hc.run_checks(_args(url))
    assert res["overall"] == "FAIL"
    assert any(c["check"] == "http_ready" and c["status"] == "FAIL" for c in res["checks"])
    _Handler.ready_code = 200


def test_fail_when_port_closed():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    res = hc.run_checks(_args(f"http://127.0.0.1:{port}"))
    assert res["overall"] == "FAIL"
    assert any(c["check"] == "tcp_api" and c["status"] == "FAIL" for c in res["checks"])


def test_disk_thresholds():
    low = hc.check_host(["/"], 1000, 1000, 101, 102)
    assert all(c["status"] == "OK" for c in low)
    high = hc.check_host(["/"], 1000, 1000, 0, 0.0001)
    assert any(c["check"].startswith("disk") and c["status"] == "FAIL" for c in high)


def test_exit_code_mapping(server, capsys):
    _Handler.ready_code = 503
    url = f"http://127.0.0.1:{server.server_port}"
    code = hc.main(["--url", url])
    assert code == 2
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["overall"] == "FAIL"
    _Handler.ready_code = 200
