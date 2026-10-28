"""A tiny webhook receiver for the demo's alert channel.

POST /hook verifies the `X-Celery-Manager-Signature` HMAC (sha256 of
"<timestamp>." + body, keyed with the shared secret) and logs the payload.
GET / shows the last 50 alerts as HTML. Standard library only.
"""

import hashlib
import hmac
import html
import json
import os
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SECRET = os.environ.get("ALERT_SINK_SECRET", "demo-webhook-secret")
MAX_AGE = 300  # reject signed requests older than this (replay protection)
alerts: deque = deque(maxlen=50)


def verify(headers, body: bytes) -> str:
    """Returns "ok" or a reason the signature was rejected."""
    timestamp = headers.get("X-Celery-Manager-Timestamp", "")
    given = headers.get("X-Celery-Manager-Signature", "")
    if not timestamp or not given:
        return "missing signature headers"
    expected = "sha256=" + hmac.new(
        SECRET.encode(), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, given):
        return "bad signature"
    try:
        if abs(time.time() - int(timestamp)) > MAX_AGE:
            return "timestamp too old"
    except ValueError:
        return "bad timestamp"
    return "ok"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _reply(self, code: int, body: bytes, kind: str = "text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", f"{kind}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/hook":
            return self._reply(404, b"not found")
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        status = verify(self.headers, body)
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {"raw": body.decode(errors="replace")}
        alerts.appendleft({"at": datetime.now(timezone.utc).strftime("%H:%M:%S"), "signature": status, "payload": payload})
        print(f"[alert] signature={status} event={payload.get('event')} summary={payload.get('summary')}", flush=True)
        if status != "ok":
            return self._reply(401, status.encode())
        self._reply(200, b"ok")

    def do_GET(self):
        if self.path != "/":
            return self._reply(404, b"not found")
        rows = "".join(
            f"<tr><td>{a['at']}</td><td>{html.escape(str(a['payload'].get('event', '')))}</td>"
            f"<td>{html.escape(str(a['payload'].get('summary', '')))}</td><td>{a['signature']}</td></tr>"
            for a in alerts
        ) or '<tr><td colspan="4">No alerts yet.</td></tr>'
        page = (
            "<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=5>"
            "<title>Alert sink</title><style>body{font:14px sans-serif;margin:2em}"
            "td,th{padding:4px 12px;text-align:left;border-bottom:1px solid #ddd}</style>"
            f"<h1>Alert sink</h1><p>Last {len(alerts)} webhook alerts, newest first.</p>"
            f"<table><tr><th>Time (UTC)</th><th>Event</th><th>Summary</th><th>Signature</th></tr>{rows}</table>"
        )
        self._reply(200, page.encode(), "text/html")


if __name__ == "__main__":
    print("alert sink listening on :9000", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 9000), Handler).serve_forever()
