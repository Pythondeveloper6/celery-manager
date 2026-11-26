"""Record outbound HTTP calls made inside tracked runs.

    from celery_manager.contrib import http
    http.instrument()            # in the worker, after celery_manager.setup()

Per call: target (scheme://host[:port]), method, status, duration, error class
and response size from Content-Length. Never the path, query string, headers
or body. At most `external_calls_max_per_run` calls are stored per run.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

logger = logging.getLogger("celery_manager")
_counts: dict[str, int] = {}
_lock = threading.Lock()
_installed: set[str] = set()


def _target(url) -> str:
    parts = urlsplit(str(url))
    host = parts.hostname or ""
    return f"{parts.scheme}://{host}" + (f":{parts.port}" if parts.port else "")


def _size(headers) -> int | None:
    try:
        value = headers.get("content-length") if headers is not None else None
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def record_call(url, method, status, started, error=None, size=None, kind="http") -> None:
    """Store one call against the current tracked run; a no-op outside one.
    Never raises."""
    try:
        from celery_manager import decorator
        from celery_manager.progress import _current_task_id
        task_id = _current_task_id.get()
        sessions = decorator._sessions()
        if not task_id or sessions is None:
            return
        config = decorator._settings()
        cap = getattr(config, "external_calls_max_per_run", 200)
        with _lock:
            if len(_counts) > 10_000:
                _counts.clear()
            _counts[task_id] = seen = _counts.get(task_id, 0) + 1
        if seen > cap:
            return  # shortcut: calls past the cap are dropped, not aggregated; raise the cap if a run needs more
        from celery import current_task

        from celery_manager.db.external import ExternalCall
        from celery_manager.telemetry import guarded
        attempt = getattr(getattr(current_task, "request", None), "retries", 0) or 0
        row = ExternalCall(task_id=task_id, attempt=attempt, kind=kind, target=_target(url)[:255],
                           method=str(method or "")[:16] or None, status=status,
                           duration_ms=(time.perf_counter() - started) * 1000,
                           error=type(error).__name__[:128] if error is not None else None,
                           bytes=size, at=datetime.now(timezone.utc))
        with guarded(config), sessions() as session:
            session.add(row)
            session.commit()
    except Exception:  # noqa: BLE001 - telemetry must never fail the call
        logger.debug("celery-manager: external call not recorded", exc_info=True)


def _wrap_sync(owner, name):
    original = getattr(owner, name)

    def send(self, request, *args, **kwargs):
        started = time.perf_counter()
        try:
            response = original(self, request, *args, **kwargs)
        except Exception as exc:
            record_call(request.url, request.method, None, started, error=exc)
            raise
        record_call(request.url, request.method, response.status_code, started, size=_size(response.headers))
        return response

    send._cm_original = original
    setattr(owner, name, send)


def _wrap_async(owner, name):
    original = getattr(owner, name)

    async def send(self, request, *args, **kwargs):
        started = time.perf_counter()
        try:
            response = await original(self, request, *args, **kwargs)
        except Exception as exc:
            record_call(request.url, request.method, None, started, error=exc)
            raise
        record_call(request.url, request.method, response.status_code, started, size=_size(response.headers))
        return response

    send._cm_original = original
    setattr(owner, name, send)


def instrument(requests: bool = True, httpx: bool = True) -> list[str]:
    """Patch the installed clients once per process. Returns what was patched."""
    done = []
    if requests and "requests" not in _installed:
        try:
            import requests as requests_lib
        except ImportError:
            pass
        else:
            _wrap_sync(requests_lib.Session, "send")
            _installed.add("requests")
            done.append("requests")
    if httpx and "httpx" not in _installed:
        try:
            import httpx as httpx_lib
        except ImportError:
            pass
        else:
            _wrap_sync(httpx_lib.Client, "send")
            _wrap_async(httpx_lib.AsyncClient, "send")
            _installed.add("httpx")
            done.append("httpx")
    return done


def uninstrument() -> None:
    """Undo `instrument` (for tests)."""
    import importlib
    for lib, owners in (("requests", ("Session",)), ("httpx", ("Client", "AsyncClient"))):
        if lib in _installed:
            module = importlib.import_module(lib)
            for owner in owners:
                cls = getattr(module, owner)
                cls.send = cls.send._cm_original
            _installed.discard(lib)
