import asyncio
import json

import pytest
from celery import Celery

from celery_manager.api.app import create_app
from celery_manager.api.sse import EventBus
from celery_manager.auth.users import create_user
from celery_manager.config import CeleryManagerConfig


def test_stream_emits_published_event():
    """Publishes an event on the app's EventBus, then drives the real
    ASGI app (create_app()'s FastAPI instance) directly through GET
    /api/stream to prove the SSE endpoint forwards it.

    We can't use fastapi.testclient.TestClient (or httpx.ASGITransport)
    for this: both fully drain the ASGI response before returning it to
    the caller, so they hang forever against a genuinely never-ending
    SSE stream. Driving the ASGI callable with our own receive/send and
    cancelling once the first chunk arrives exercises the exact same
    production code path (create_app -> EventBus -> sse_stream ->
    StreamingResponse) without that deadlock.
    """
    config = CeleryManagerConfig(celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret")
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "alice", "pw")

    app.state.cm_event_bus.publish({"task_id": "t1", "status": "succeeded"})

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "path": "/api/stream",
        "raw_path": b"/api/stream",
        "root_path": "",
        "scheme": "http",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 123),
        "server": ("testserver", 80),
        "state": {},
    }

    async def run():
        messages: asyncio.Queue = asyncio.Queue()
        request_sent = False

        async def receive():
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await asyncio.sleep(3600)  # never disconnect on our own

        async def send(message):
            await messages.put(message)

        task = asyncio.create_task(app(scope, receive, send))
        try:
            start = await asyncio.wait_for(messages.get(), timeout=5)
            assert start["type"] == "http.response.start"
            assert start["status"] == 200

            body_message = await asyncio.wait_for(messages.get(), timeout=5)
            assert body_message["type"] == "http.response.body"
            text = body_message["body"].decode()
            line = text.split("\n", 1)[0]
            assert line.startswith("data: ")
            payload = json.loads(line[len("data: "):])
            assert payload == {"task_id": "t1", "status": "succeeded"}
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run())


# --- I1 (partial): EventBus.publish is called from the events listener's
# thread, not the thread running the event loop that owns the subscriber
# queues. asyncio.Queue is not thread-safe and put_nowait from a foreign
# thread does not wake the loop awaiting it. ---


def test_publish_from_a_foreign_thread_wakes_the_waiting_consumer_promptly():
    """The bug is not "raises an error" — it's "the event loop is never woken".

    A bare `queue.put_nowait()` from a foreign thread marks the waiter's
    future done via `loop.call_soon`, which does NOT write to the loop's
    self-pipe. A loop parked in `select()` therefore keeps sleeping until some
    unrelated timer fires. So asserting mere eventual delivery is not enough —
    a generous timeout hides the bug entirely. This asserts delivery is
    *prompt*: the publisher sleeps first so the loop is genuinely parked, and
    the event must arrive well before the only other scheduled wakeup.
    """
    import threading
    import time

    bus = EventBus()
    publish_delay = 0.3
    # The only timer on the loop. With the fix, delivery happens at
    # ~publish_delay. Without it, the loop sleeps until this fires instead.
    only_other_wakeup = 10.0

    async def consume():
        queue = bus.subscribe()

        def publisher():
            time.sleep(publish_delay)  # let the loop actually park in select()
            bus.publish({"task_id": "t9", "type": "task-succeeded"})

        threading.Thread(target=publisher, daemon=True).start()

        started = time.monotonic()
        event = await asyncio.wait_for(queue.get(), timeout=only_other_wakeup)
        return event, time.monotonic() - started

    received, elapsed = asyncio.run(consume())
    assert received == {"task_id": "t9", "type": "task-succeeded"}
    assert elapsed < 3.0, f"consumer woke after {elapsed:.1f}s — the loop was not woken by publish()"


def test_publish_reaches_every_subscriber_and_stops_after_unsubscribe():
    async def run():
        bus = EventBus()
        first = bus.subscribe()
        second = bus.subscribe()

        bus.publish({"n": 1})
        assert await asyncio.wait_for(first.get(), timeout=5) == {"n": 1}
        assert await asyncio.wait_for(second.get(), timeout=5) == {"n": 1}

        bus.unsubscribe(first)
        bus.publish({"n": 2})
        assert await asyncio.wait_for(second.get(), timeout=5) == {"n": 2}
        assert first.empty()

    asyncio.run(run())


def test_publish_before_any_subscriber_is_buffered_and_replayed():
    """Pre-existing behaviour the thread-safety rework must not regress."""

    async def run():
        bus = EventBus()
        bus.publish({"n": "early"})  # no subscribers yet, and no running-loop context needed
        queue = bus.subscribe()
        assert await asyncio.wait_for(queue.get(), timeout=5) == {"n": "early"}

    asyncio.run(run())
