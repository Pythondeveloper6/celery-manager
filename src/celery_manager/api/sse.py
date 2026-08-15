from __future__ import annotations

import asyncio
import json


class EventBus:
    """In-process pub/sub for task-status-change events. The events
    listener (Task 4) publishes here; the /api/stream endpoint below
    subscribes and forwards to connected browsers as SSE."""

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue] = []
        self._pending: list[dict] = []  # buffer for publishes before any subscriber connects (used in tests)

    def publish(self, event: dict) -> None:
        if not self._subscribers:
            self._pending.append(event)
            return
        for queue in self._subscribers:
            queue.put_nowait(event)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        for event in self._pending:
            queue.put_nowait(event)
        self._pending.clear()
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)


async def sse_stream(bus: EventBus):
    queue = bus.subscribe()
    try:
        while True:
            event = await queue.get()
            yield f"data: {json.dumps(event, default=str)}\n\n"
    finally:
        bus.unsubscribe(queue)
