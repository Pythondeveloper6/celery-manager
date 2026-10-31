"""Dramatiq middleware that records every message as an observed run.

    import dramatiq
    from celery_manager.adapters.dramatiq import CeleryManagerMiddleware

    broker.add_middleware(CeleryManagerMiddleware(RunObserver(config)))

Run identity is `<message_id>:<retry>`, so each Dramatiq retry is its own
run and a retry after a failure never collides with the failed attempt.
Enqueueing records `queued` in the producing process; the worker records
`started` and the outcome. A message skipped by Dramatiq (for example by the
age limit) is recorded as skipped.
"""
from __future__ import annotations

import threading
from typing import Any

import dramatiq

from celery_manager.observation import ObservedRun, RunObserver


def _run_id(message) -> str:
    return f"{message.message_id}:{message.options.get('retries', 0)}"


class CeleryManagerMiddleware(dramatiq.Middleware):
    def __init__(self, observer: RunObserver, *, record_enqueue: bool = True) -> None:
        self.observer = observer
        self.record_enqueue = record_enqueue
        self._runs: dict[str, ObservedRun] = {}
        self._lock = threading.Lock()

    def _observe(self, message) -> ObservedRun:
        return self.observer.observe(message.actor_name, run_id=_run_id(message),
                                     context={"queue": message.queue_name})

    def after_enqueue(self, broker, message, delay) -> None:
        if self.record_enqueue:
            self._observe(message)._write("queued")

    def before_process_message(self, broker, message) -> None:
        run = self._observe(message)
        with self._lock:
            self._runs[message.message_id] = run
        run.start()

    def after_process_message(self, broker, message, *, result: Any = None, exception: BaseException | None = None) -> None:
        with self._lock:
            run = self._runs.pop(message.message_id, None)
        if run is None:
            return
        if exception is None:
            run.success(result)
        else:
            run.fail(exception)

    def after_skip_message(self, broker, message) -> None:
        with self._lock:
            run = self._runs.pop(message.message_id, None)
        (run or self._observe(message)).skip("Skipped by Dramatiq")
