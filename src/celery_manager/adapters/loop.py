"""Observe jobs run by a hand-written loop: asyncio tasks, cron-like loops,
`while True: ...; sleep(n)` threads.

    observer = RunObserver(config)

    # Wrap an existing job; every call becomes one observed run.
    @observed_job(observer, "reports.nightly")
    async def nightly(): ...

    # Or let celery-manager drive a fixed-interval loop for you.
    asyncio.create_task(run_every(observer, "cache.refresh", refresh, seconds=300))

A failing job is recorded as failed and logged; `run_every` keeps going, as
a scheduler loop should. Cancelling the loop task records the in-flight run
as interrupted and stops the loop.
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import logging
import threading
from collections.abc import Awaitable, Callable
from typing import Any

from celery_manager.observation import RunObserver

logger = logging.getLogger(__name__)


def observed_job(observer: RunObserver, name: str, **observe_kwargs: Any):
    """Decorator: each call of the sync or async function is one observed run.

    Equivalent to `observer.observe(name, ...)` used as a decorator, spelled
    out for loop-based code that has no other integration point.
    """
    return observer.observe(name, **observe_kwargs)


async def run_every(
    observer: RunObserver,
    name: str,
    job: Callable[[], Any] | Callable[[], Awaitable[Any]],
    *,
    seconds: float,
    stop: asyncio.Event | None = None,
    run_immediately: bool = True,
    context: dict | None = None,
    max_runs: int | None = None,
) -> int:
    """Run `job` every `seconds` (start to start) until `stop` is set or the
    task is cancelled, recording each run. Returns the number of runs.

    A sync `job` runs in a worker thread so it cannot block the event loop.
    A run that overruns the interval is followed immediately by the next one;
    missed slots are not replayed.
    """
    if seconds <= 0:
        raise ValueError("seconds must be positive")
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    runs = 0
    next_at = loop.time() + (0 if run_immediately else seconds)
    while not stop.is_set() and (max_runs is None or runs < max_runs):
        delay = next_at - loop.time()
        if delay > 0:
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
                break
            except asyncio.TimeoutError:
                pass
        next_at = max(next_at + seconds, loop.time())
        runs += 1
        try:
            async with observer.observe(name, context=context):
                if inspect.iscoroutinefunction(job):
                    await job()
                else:
                    await asyncio.to_thread(job)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("celery-manager: observed loop job %s failed; the loop continues", name)
    return runs


def run_every_in_thread(
    observer: RunObserver,
    name: str,
    job: Callable[[], Any],
    *,
    seconds: float,
    stop: threading.Event | None = None,
    context: dict | None = None,
) -> threading.Thread:
    """Start a daemon thread that runs sync `job` every `seconds` until
    `stop` is set, recording each run. Returns the started thread."""
    if seconds <= 0:
        raise ValueError("seconds must be positive")
    stop = stop or threading.Event()

    @functools.wraps(job)
    def body():
        while not stop.is_set():
            try:
                with observer.observe(name, context=context):
                    job()
            except Exception:
                logger.exception("celery-manager: observed loop job %s failed; the loop continues", name)
            stop.wait(seconds)

    thread = threading.Thread(target=body, name=f"cm-loop-{name}", daemon=True)
    thread.stop_event = stop  # type: ignore[attr-defined]
    thread.start()
    return thread
