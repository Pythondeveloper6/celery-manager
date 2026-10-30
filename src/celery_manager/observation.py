"""Observe host-owned work, without scheduling, retrying or executing it."""
from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import re
import threading
import uuid
import weakref
from contextvars import ContextVar
from datetime import datetime, timezone
from itertools import islice

from sqlalchemy import select

from celery_manager.db.identity import exact_identity, portable_text_fits
from celery_manager.db.models import TaskRun
from celery_manager.db.runs import lock_task_run
from celery_manager.runtime import setup

logger = logging.getLogger(__name__)
_context = ContextVar("cm_observation_context", default=None)
_current_run = ContextVar("cm_observed_run", default=None)
_UNSET = object()
_TERMINAL = {"succeeded", "failed", "skipped", "interrupted"}
_SENSITIVE = re.compile(r"password|passwd|secret|token|authorization|cookie|api.?key|credential", re.IGNORECASE)
_SECRET_VALUE = re.compile(r"(?i)\b(password|passwd|secret|token|authorization|api.?key)\s*[:=]\s*(?:(?:bearer|basic)\s+)?\S+")


class ObservationError(RuntimeError):
    """Observation cannot be persisted; host execution is never retried."""


def current_run():
    """The current explicit observation handle, scoped to this async task/thread."""
    return _current_run.get()


def _identifier(value, label, *, nullable=True, limit=255):
    if value is None and nullable:
        return None
    if not portable_text_fits(value, limit) or not value or not value.isprintable():
        raise ValueError(f"{label} must contain 1 to {limit} printable UTF-16 code units")
    return value


def _clean(value, *, depth=0, budget=None):
    budget = [256] if budget is None else budget
    budget[0] -= 1
    if budget[0] < 0:
        return "[item limit]"
    if depth >= 6:
        return "[depth limit]"
    if isinstance(value, int) and value.bit_length() > 4096:
        return "[large integer]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        import math
        return value if math.isfinite(value) else "[non-finite]"
    if isinstance(value, str):
        return _SECRET_VALUE.sub(lambda match: match.group(1) + "=[redacted]", value[:2048])
    if isinstance(value, dict):
        result = {}
        for key, item in islice(value.items(), 50):
            if isinstance(key, str):
                key = key[:128]
                result[key] = "[redacted]" if _SENSITIVE.search(key) else _clean(item, depth=depth + 1, budget=budget)
        return result
    if isinstance(value, (tuple, list)):
        return [_clean(item, depth=depth + 1, budget=budget) for item in value[:50]]
    # Never invoke arbitrary __repr__/serialization on host objects.
    return "[unsupported value]"


class ObservationContext:
    def __init__(self, *, tenant_id=_UNSET, correlation_id=_UNSET, operation_key=_UNSET, metadata=None):
        self.values = {"metadata": metadata or {}}
        if tenant_id is not _UNSET:
            self.values["tenant_id"] = _identifier(tenant_id, "tenant_id")
        if correlation_id is not _UNSET:
            self.values["correlation_id"] = _identifier(correlation_id, "correlation_id")
        if operation_key is not _UNSET:
            self.values["operation_key"] = _identifier(operation_key, "operation_key")
        self.token = None

    def __enter__(self):
        previous = _context.get() or {}
        values = {**previous, **self.values}
        values["metadata"] = {**previous.get("metadata", {}), **self.values["metadata"]}
        self.token = _context.set(values)
        return self

    def __exit__(self, *exc):
        _context.reset(self.token)

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, *exc):
        self.__exit__(*exc)


class RunObserver:
    """Durable, fail-open telemetry for execution owned by the host application.

    Construction does no database work. Async lifecycle methods perform all
    database initialization and persistence in a worker thread.
    """

    def __init__(self, config, *, fail_open=None):
        self.config = config
        self.fail_open = config.observer_fail_open if fail_open is None else fail_open
        self._lock = threading.Lock()
        self._health = {"writes_succeeded": 0, "writes_failed": 0, "last_success_at": None,
                        "last_error_at": None, "last_error_type": None}
        observers = getattr(config, "_cm_observers", None)
        if observers is None:
            observers = weakref.WeakSet()
            config._cm_observers = observers
        observers.add(self)

    context = staticmethod(ObservationContext)

    def health(self):
        with self._lock:
            return dict(self._health)

    def observe(self, name, *, run_id=None, tenant_id=_UNSET, context=None, operation_key=_UNSET, correlation_id=_UNSET, outcome=None):
        inherited = _context.get() or {}
        handle = ObservedRun(self, name, run_id=run_id,
                           tenant_id=inherited.get("tenant_id") if tenant_id is _UNSET else tenant_id,
                           correlation_id=inherited.get("correlation_id") if correlation_id is _UNSET else correlation_id,
                           metadata={**inherited.get("metadata", {}), **(context or {})},
                           operation_key=inherited.get("operation_key") if operation_key is _UNSET else operation_key)
        handle._options = {"tenant_id": tenant_id, "correlation_id": correlation_id,
                           "context": context, "operation_key": operation_key, "outcome": outcome}
        handle._explicit_id = run_id is not None
        return handle

    def queued(self, name, **kwargs):
        run = self.observe(name, **kwargs)
        run._write("queued")
        return run

    async def aqueued(self, name, **kwargs):
        run = self.observe(name, **kwargs)
        await run._awrite("queued")
        return run

    def duplicates(self, operation_key, *, tenant_id=_UNSET, limit=100):
        """Visibility only: equal operation keys never suppress host execution."""
        _identifier(operation_key, "operation_key", nullable=False)
        tenant_id = (_context.get() or {}).get("tenant_id") if tenant_id is _UNSET else tenant_id
        _identifier(tenant_id, "tenant_id")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("limit must be 1 to 500")
        def read():
            with setup(self.config)() as session:
                rows = session.scalars(select(TaskRun).where(
                    TaskRun.source == "external", exact_identity(TaskRun.tenant_id, tenant_id),
                    exact_identity(TaskRun.operation_key, operation_key)).order_by(TaskRun.id.desc()).limit(limit)).all()
                return [{"run_id": row.task_id, "name": row.task_name, "status": row.status,
                         "tenant_id": row.tenant_id, "operation_key": row.operation_key} for row in rows]
        return self._perform(read, fallback=[])

    async def aduplicates(self, operation_key, **kwargs):
        return await asyncio.to_thread(self.duplicates, operation_key, **kwargs)

    def _perform(self, operation, *, fallback=None, preserve_error=False):
        try:
            value = operation()
        except Exception as exc:
            with self._lock:
                self._health["writes_failed"] += 1
                self._health.update(last_error_at=datetime.now(timezone.utc).isoformat(), last_error_type=type(exc).__name__)
            logger.warning("celery-manager observation telemetry failed (%s); host work is unaffected", type(exc).__name__)
            callback = self.config.observer_error_callback
            if callback is not None:
                try:
                    callback(self.health())
                except Exception:  # noqa: BLE001 - telemetry hooks must not affect host work
                    logger.warning("celery-manager observation error callback failed")
            if not self.fail_open and not preserve_error:
                raise ObservationError("Observation persistence failed; inspect observer.health()") from exc
            return fallback
        with self._lock:
            self._health["writes_succeeded"] += 1
            self._health["last_success_at"] = datetime.now(timezone.utc).isoformat()
        return value


class ObservedRun:
    def __init__(self, observer, name, *, run_id, tenant_id, correlation_id, metadata, operation_key):
        self.observer = observer
        self.name = _identifier(name, "name", nullable=False)
        raw = run_id if run_id is not None else str(uuid.uuid4())
        _identifier(raw, "run_id", nullable=False)
        _identifier(raw, "run_id", nullable=False, limit=246 if not raw.startswith("external:") else 255)
        self.run_id = raw if raw.startswith("external:") else "external:" + raw
        self.tenant_id = _identifier(tenant_id, "tenant_id")
        self.correlation_id = _identifier(correlation_id, "correlation_id")
        self.operation_key = _identifier(operation_key, "operation_key")
        if not isinstance(metadata, dict):
            raise TypeError("context must be a mapping")
        allowed = observer.config.observer_metadata_keys
        if allowed is not None:
            metadata = {key: value for key, value in metadata.items() if key in allowed}
        clean = _clean(metadata)
        limit = observer.config.observer_context_max_bytes
        text = json.dumps(clean, ensure_ascii=True, allow_nan=False)
        if len(text.encode()) > limit:
            clean = {"_truncated": True}
            text = json.dumps(clean)
        self.context_json = text
        self._terminal = False
        self._token = None
        self._context_token = None

    def _transition(self, status, *, reason=None, result=None):
        at = datetime.now(timezone.utc)
        factory = setup(self.observer.config)
        with factory() as session:
            row = lock_task_run(session, self.run_id, seed={
                "task_name": self.name, "source": "external", "status": "queued",
                "tenant_id": self.tenant_id, "operation_key": self.operation_key,
                "correlation_id": self.correlation_id, "context_json": self.context_json, "sent_at": at})
            if (row.task_id != self.run_id or row.source != "external" or row.tenant_id != self.tenant_id or row.task_name != self.name
                    or row.operation_key != self.operation_key or row.correlation_id != self.correlation_id):
                raise ObservationError("Run identity collision; source and tenant cannot be reassigned")
            if row.status in _TERMINAL:
                if status in ("queued", row.status):
                    return
                raise ObservationError("A terminal observation cannot be restarted or overwritten")
            if status == "queued" and row.status == "started":
                return
            row.status = status
            if status == "started" and row.started_at is None:
                row.started_at = at
            if status in _TERMINAL:
                row.finished_at = at
                if row.started_at is not None:
                    start = row.started_at.replace(tzinfo=timezone.utc) if row.started_at.tzinfo is None else row.started_at
                    row.runtime_seconds = max(0, (at - start).total_seconds())
                if status == "skipped":
                    row.skip_reason = reason[:255] if reason else reason
                elif status in ("failed", "interrupted"):
                    row.exception = reason
                if status == "succeeded" and self.observer.config.observer_store_results:
                    from celery_manager.logcapture import result_fields
                    for key, value in result_fields(_clean(result), self.observer.config).items():
                        setattr(row, key, value)
            session.commit()

    def _write(self, status, *, preserve_error=False, reason=None, result=None):
        # Sanitize exception text before it can enter storage or error hooks.
        reason = _clean(reason)[:2048] if isinstance(reason, str) else None
        self.observer._perform(lambda: self._transition(status, reason=reason, result=result), preserve_error=preserve_error)
        if status in _TERMINAL:
            self._terminal = True
        return self

    async def _awrite(self, status, **kwargs):
        return await asyncio.to_thread(self._write, status, **kwargs)

    def start(self):
        return self._write("started")

    async def astart(self):
        return await self._awrite("started")

    def success(self, result=None):
        return self._write("succeeded", result=result)

    async def asuccess(self, result=None):
        return await self._awrite("succeeded", result=result)

    def fail(self, exc):
        return self._write("failed", reason=_exception_text(exc))

    async def afail(self, exc):
        return await self._awrite("failed", reason=_exception_text(exc))

    def skip(self, reason="Skipped by host"):
        return self._write("skipped", reason=reason)

    async def askip(self, reason="Skipped by host"):
        return await self._awrite("skipped", reason=reason)

    def interrupt(self, reason="Host reported interrupted execution"):
        return self._write("interrupted", reason=reason)

    async def ainterrupt(self, reason="Host reported interrupted execution"):
        return await self._awrite("interrupted", reason=reason)

    def __enter__(self):
        self.start()
        self._token = _current_run.set(self)
        self._context_token = _context.set({"tenant_id": self.tenant_id, "correlation_id": self.correlation_id,
                                           "operation_key": self.operation_key, "metadata": json.loads(self.context_json)})
        return self

    def __exit__(self, kind, exc, tb):
        try:
            if not self._terminal:
                status = "succeeded" if exc is None else "interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)) else "failed"
                self._write(status, reason=_exception_text(exc) if exc is not None else None, preserve_error=exc is not None)
        finally:
            _context.reset(self._context_token)
            _current_run.reset(self._token)
        return False

    async def __aenter__(self):
        try:
            await self.astart()
        except asyncio.CancelledError:
            await asyncio.shield(self._awrite("interrupted", reason="Cancelled before observed body entered", preserve_error=True))
            raise
        self._token = _current_run.set(self)
        self._context_token = _context.set({"tenant_id": self.tenant_id, "correlation_id": self.correlation_id,
                                           "operation_key": self.operation_key, "metadata": json.loads(self.context_json)})
        return self

    async def __aexit__(self, kind, exc, tb):
        try:
            if not self._terminal:
                status = "succeeded" if exc is None else "interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)) else "failed"
                # Cancellation must never turn into a success, swallow the
                # host's cancellation or cancel the pending DB write itself.
                await asyncio.shield(self._awrite(status, reason=_exception_text(exc) if exc is not None else None,
                                                preserve_error=exc is not None))
        finally:
            _context.reset(self._context_token)
            _current_run.reset(self._token)
        return False

    def __call__(self, function):
        # A reusable decorator creates a fresh identity in the *calling*
        # context for every invocation, including concurrent async calls.
        if self._explicit_id:
            raise ValueError("A decorator requires a new run_id per invocation; use a context manager for explicit IDs")
        def fresh():
            return self.observer.observe(self.name, **self._options)
        def classified(run, value):
            outcome = self._options.get("outcome")
            if outcome is None:
                return "succeeded"
            def resolve():
                status = outcome(value)
                if status not in _TERMINAL:
                    raise ValueError("outcome must return succeeded, failed, skipped or interrupted")
                return status
            return self.observer._perform(resolve, fallback="interrupted")
        if inspect.iscoroutinefunction(function):
            @functools.wraps(function)
            async def asynchronous(*args, **kwargs):
                async with fresh() as run:
                    value = await function(*args, **kwargs)
                    if not run._terminal:
                        status = classified(run, value)
                        await run._awrite(status, result=value, reason=None if status == "succeeded" else "Host result classification: " + status)
                    return value
            return asynchronous
        @functools.wraps(function)
        def synchronous(*args, **kwargs):
            with fresh() as run:
                value = function(*args, **kwargs)
                if not run._terminal:
                    status = classified(run, value)
                    run._write(status, result=value, reason=None if status == "succeeded" else "Host result classification: " + status)
                return value
        return synchronous


def _exception_text(exc):
    try:
        return type(exc).__name__ + ": " + str(exc)
    except Exception:  # noqa: BLE001 - exception text is optional telemetry
        return type(exc).__name__ + ": [unavailable message]"
