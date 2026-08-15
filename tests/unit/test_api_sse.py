import asyncio
import json

import pytest
from celery import Celery

from celery_manager.config import CeleryManagerConfig
from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user


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
