"""Liveness of the events listener: counters, a durable heartbeat row and the
probes a container platform can read without credentials."""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sqlalchemy.exc import IntegrityError

HEARTBEAT_KEY = "listener_heartbeat"
HEARTBEAT_SECONDS = 15
# No event for this long means the listener is connected to nothing useful.
SILENT_SECONDS = 120
log = logging.getLogger(__name__)
_heartbeat_write_lock = threading.Lock()


class ListenerStats:
    """Counters shared by the capture thread and the heartbeat thread."""

    def __init__(self):
        self.instance_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.started = time.monotonic()
        self.last_event_mono = self.started
        self.last_event_at: datetime | None = None
        self.processed = 0
        self.failed = 0
        self.reconnects = 0
        self.clock_skew = 0
        self.lag_seconds: float | None = None

    def healthy(self) -> bool:
        return time.monotonic() - self.last_event_mono < SILENT_SECONDS

    def snapshot(self) -> dict:
        return {
            "at": datetime.now(timezone.utc).isoformat(),
            "instance_id": self.instance_id,
            "last_event_at": self.last_event_at.isoformat() if self.last_event_at else None,
            "events_processed": self.processed,
            "events_failed": self.failed,
            "reconnects": self.reconnects,
            "clock_skew": self.clock_skew,
            "lag_seconds": self.lag_seconds,
        }


def write_heartbeat(session_factory, stats: ListenerStats) -> None:
    from celery_manager.db.models import AppSetting

    now = datetime.now(timezone.utc)
    value = json.dumps(stats.snapshot())
    # The capture loop and heartbeat thread can both initialize the row when
    # the first event arrives during startup. Serialize local writers and
    # retry a concurrent first insert from another listener process.
    with _heartbeat_write_lock:
        for attempt in range(2):
            try:
                with session_factory() as session:
                    row = session.get(AppSetting, HEARTBEAT_KEY)
                    if row is None:
                        row = AppSetting(key=HEARTBEAT_KEY)
                        session.add(row)
                    row.value, row.updated_at, row.updated_by = value, now, "events"
                    session.commit()
                return
            except IntegrityError:
                if attempt:
                    raise


def _serve_probe(port: int, stats: ListenerStats) -> ThreadingHTTPServer:
    class Probe(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if stats.healthy() else 503)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), Probe)
    threading.Thread(target=server.serve_forever, name="cm-listener-probe", daemon=True).start()
    return server


def start_heartbeat(session_factory, stats: ListenerStats, stop: threading.Event, *,
                    health_file: str | None = None, health_port: int | None = None):
    """Write the heartbeat every HEARTBEAT_SECONDS on a daemon thread; touch
    `health_file` while healthy; serve `health_port` (200 or 503). Returns a
    callable that shuts the probe server down."""
    server = _serve_probe(health_port, stats) if health_port else None

    def beat():
        while True:
            try:
                write_heartbeat(session_factory, stats)
            except Exception:
                log.warning("celery-manager: could not write the listener heartbeat", exc_info=True)
            if health_file and stats.healthy():
                try:
                    with open(health_file, "a"):
                        os.utime(health_file)
                except OSError:
                    log.warning("celery-manager: could not touch the health file")
            if stop.wait(HEARTBEAT_SECONDS):
                return

    threading.Thread(target=beat, name="cm-listener-heartbeat", daemon=True).start()
    return server.shutdown if server else (lambda: None)
