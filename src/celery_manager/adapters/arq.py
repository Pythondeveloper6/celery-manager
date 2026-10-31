"""Observe arq jobs.

arq's `on_job_start`/`on_job_end` hooks do not receive the job's outcome, so
the integration wraps the job function itself:

    from celery_manager.adapters.arq import observed

    observe = observed(RunObserver(config))

    @observe
    async def send_invoice(ctx, invoice_id): ...

    class WorkerSettings:
        functions = [send_invoice]

Run identity is `<job_id>:<job_try>`, so each retry is its own run. arq's
`Retry` exception is recorded as failed for that attempt; cancellation (job
timeout or shutdown) is recorded as interrupted. The wrapper re-raises
everything, so arq's retry and timeout behaviour is unchanged. Nothing here
imports arq.
"""
from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from celery_manager.observation import RunObserver

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


def observed(observer: RunObserver, *, name: str | None = None) -> Callable[[F], F]:
    def decorate(function: F) -> F:
        job_name = name or f"{function.__module__}.{function.__qualname__}"

        @functools.wraps(function)
        async def wrapper(ctx, *args, **kwargs):
            job_id = ctx.get("job_id") if isinstance(ctx, dict) else None
            run_id = f"{job_id}:{ctx.get('job_try', 1)}" if job_id else None
            async with observer.observe(job_name, run_id=run_id) as run:
                value = await function(ctx, *args, **kwargs)
                await run.asuccess(value)
                return value

        return wrapper  # type: ignore[return-value]

    return decorate
