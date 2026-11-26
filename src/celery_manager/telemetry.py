"""A per-process circuit breaker for writes made inside tasks and requests.

After `telemetry_breaker_failures` consecutive failures, telemetry writes are
skipped for `telemetry_breaker_seconds`; then one write is let through as a
probe. A slow or unreachable monitoring database then costs a task or a
request nothing instead of one driver timeout per write.
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager

logger = logging.getLogger("celery_manager")


class Breaker:
    def __init__(self, failures: int = 3, seconds: float = 30.0, clock=time.monotonic):
        self.failures, self.seconds, self.clock = failures, seconds, clock
        self.consecutive = 0
        self.opened_at: float | None = None
        self.skipped = 0
        self.lock = threading.Lock()

    def allow(self) -> bool:
        with self.lock:
            if self.opened_at is None:
                return True
            if self.clock() - self.opened_at >= self.seconds:
                self.opened_at = self.clock()  # one probe per period
                return True
            self.skipped += 1
            return False

    def record(self, ok: bool) -> None:
        with self.lock:
            if ok:
                self.consecutive, self.opened_at = 0, None
                return
            self.consecutive += 1
            if self.failures and self.consecutive >= self.failures:
                if self.opened_at is None:
                    logger.warning("celery-manager: telemetry writes paused for %ss after %s failures",
                                   self.seconds, self.consecutive)
                self.opened_at = self.clock()

    @property
    def open(self) -> bool:
        return self.opened_at is not None


_breakers: dict[int, Breaker] = {}
_lock = threading.Lock()


def breaker_for(config) -> Breaker:
    """The breaker for `config` in this process (a shared default without one)."""
    with _lock:
        key = id(config)
        if key not in _breakers:
            _breakers[key] = Breaker(getattr(config, "telemetry_breaker_failures", 3),
                                     getattr(config, "telemetry_breaker_seconds", 30.0))
        return _breakers[key]


class Skipped(Exception):
    """The breaker is open; the write was not attempted."""


@contextmanager
def guarded(config):
    """Run a telemetry write under the breaker: raises `Skipped` while open,
    records success or failure otherwise (and re-raises the failure)."""
    breaker = breaker_for(config)
    if not breaker.allow():
        raise Skipped
    try:
        yield
    except Exception:
        breaker.record(False)
        raise
    breaker.record(True)


def timeout_options(db_url: str, seconds: float | None) -> dict:
    """Engine options that bound connect, pool wait and statement time to
    `seconds` for the drivers celery-manager supports. Empty when None."""
    if not seconds:
        return {}
    ms = int(seconds * 1000)
    options: dict = {"pool_timeout": seconds}
    if db_url.startswith("sqlite"):
        connect = {"timeout": seconds}
    elif db_url.startswith("postgresql"):
        connect = {"connect_timeout": max(1, int(seconds)), "options": f"-c statement_timeout={ms}"}
    elif db_url.startswith(("mysql", "mariadb")):
        connect = {"connect_timeout": max(1, int(seconds)), "read_timeout": max(1, int(seconds)),
                   "write_timeout": max(1, int(seconds))}
    elif db_url.startswith("mssql"):
        connect = {"timeout": max(1, int(seconds))}
    else:
        connect = {}
    if connect:
        options["connect_args"] = connect
    return options
