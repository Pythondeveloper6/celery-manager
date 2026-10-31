"""Taskiq middleware that records every task as an observed run.

    from celery_manager.adapters.taskiq import CeleryManagerMiddleware

    broker = broker.with_middlewares(CeleryManagerMiddleware(RunObserver(config)))

`post_send` records `queued` in the producer, `pre_execute` records
`started` in the worker and `post_execute` the outcome from Taskiq's result
(`is_err` and `error`). Database writes run in a thread, never on the event
loop.
"""
from __future__ import annotations

import threading

from taskiq import TaskiqMessage, TaskiqMiddleware, TaskiqResult

from celery_manager.observation import ObservedRun, RunObserver


class CeleryManagerMiddleware(TaskiqMiddleware):
    def __init__(self, observer: RunObserver, *, record_send: bool = True) -> None:
        super().__init__()
        self.observer = observer
        self.record_send = record_send
        self._runs: dict[str, ObservedRun] = {}
        self._lock = threading.Lock()

    def _observe(self, message: TaskiqMessage) -> ObservedRun:
        return self.observer.observe(message.task_name, run_id=message.task_id)

    async def post_send(self, message: TaskiqMessage) -> None:
        if self.record_send:
            await self._observe(message)._awrite("queued")

    async def pre_execute(self, message: TaskiqMessage) -> TaskiqMessage:
        run = self._observe(message)
        with self._lock:
            self._runs[message.task_id] = run
        await run.astart()
        return message

    async def post_execute(self, message: TaskiqMessage, result: TaskiqResult) -> None:
        with self._lock:
            run = self._runs.pop(message.task_id, None)
        if run is None:
            return
        if result.is_err:
            error = result.error if isinstance(result.error, BaseException) else RuntimeError(str(result.error))
            await run.afail(error)
        else:
            await run.asuccess(result.return_value)
