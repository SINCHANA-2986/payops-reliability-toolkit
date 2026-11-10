#!/usr/bin/env python3
"""Minimal Alertmanager webhook receiver: prints each alert and appends it to a JSONL file."""
import json
import os
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

LOG = os.getenv("ALERT_LOG", "/tmp/alerts.jsonl")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {}
        for a in payload.get("alerts", []):
            line = {"received": round(time.time(), 3), "status": a.get("status"),
                    "alertname": a.get("labels", {}).get("alertname"),
                    "severity": a.get("labels", {}).get("severity"),
                    "summary": a.get("annotations", {}).get("summary")}
            print(json.dumps(line), flush=True)
            with open(LOG, "a") as f:
                f.write(json.dumps(line) + "\n")
        self.send_response(200)
        self.end_headers()


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", int(os.getenv("PORT", "5001"))), Handler).serve_forever()
