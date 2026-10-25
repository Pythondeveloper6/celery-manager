from __future__ import annotations

import asyncio
import json
import logging
import threading

logger = logging.getLogger(__name__)

# Idle streams get a comment frame this often. Proxies and load balancers close
# connections that stay silent (nginx's proxy_read_timeout defaults to 60s), and
# EventSource would then reconnect in a loop. Read at call time, so tests can shrink it.
KEEPALIVE_SECONDS = 15.0


class EventBus:
    """In-process pub/sub for task-status-change events. Publishers push here;
    the /api/stream endpoint below subscribes and forwards to connected
    browsers as SSE.

    Thread-safety matters here because publishers are not necessarily on the
    event loop's thread. `asyncio.Queue` is **not** thread-safe, and calling
    `put_nowait` on one from a foreign thread does not wake the loop waiting
    on it — the item can sit in the queue unnoticed, or corrupt the queue's
    internal waiter bookkeeping. So each subscriber is stored together with
    the event loop that owns its queue (captured in `subscribe`, which always
    runs inside that loop), and `publish` hands the work back to that loop via
    `call_soon_threadsafe`, which is the supported cross-thread entry point.

    Note the standing limitation this does *not* solve: this bus lives in the
    API process's memory, while the documented deployment runs the events
    listener as a separate OS process, which cannot reach these queues at all.
    See plan.md's Backlog ("Cross-process SSE delivery") — the real fix is
    Redis pub/sub or an in-process listener. The dashboard's task list carries
    a polling fallback in the meantime.
    """

    def __init__(self) -> None:
        self._subscribers: list[tuple[asyncio.Queue, asyncio.AbstractEventLoop]] = []
        self._pending: list[dict] = []  # buffer for publishes before any subscriber connects (used in tests)
        self._lock = threading.Lock()

    def publish(self, event: dict) -> None:
        """Safe to call from any thread."""
        with self._lock:
            if not self._subscribers:
                self._pending.append(event)
                return
            subscribers = list(self._subscribers)

        for queue, loop in subscribers:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, event)
            except RuntimeError:
                # The loop shut down between our snapshot and this call — the
                # subscriber is gone, so dropping its copy is correct. Never
                # let a dead subscriber break delivery to the live ones.
                logger.debug("celery-manager: dropped an SSE event for a closed event loop")

    def subscribe(self) -> asyncio.Queue:
        """Must be called from inside the event loop that will consume the
        returned queue — `sse_stream` below satisfies that by construction."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        with self._lock:
            for event in self._pending:
                queue.put_nowait(event)
            self._pending.clear()
            self._subscribers.append((queue, loop))
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers = [entry for entry in self._subscribers if entry[0] is not queue]


async def sse_stream(bus: EventBus):
    queue = bus.subscribe()
    try:
        while True:
            try:
                # Cancelling queue.get() on timeout is safe: no item is consumed.
                event = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            yield f"data: {json.dumps(event, default=str)}\n\n"
    finally:
        bus.unsubscribe(queue)
