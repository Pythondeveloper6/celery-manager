"""The Celery app the held-task integration tests run real workers with.

Imported by the test (to send tasks) and by the worker subprocess (`celery
-A tests.integration.held_tasks_app worker`), so both agree on the broker:
`CM_HELD_BROKER_URL`, with results in `CM_HELD_BACKEND_URL` when set.
"""

from __future__ import annotations

import os
import time

from celery import Celery

app = Celery(
    "held-tasks",
    broker=os.environ.get("CM_HELD_BROKER_URL", "memory://"),
    backend=os.environ.get("CM_HELD_BACKEND_URL") or None,
)
app.conf.update(
    worker_prefetch_multiplier=1,
    task_send_sent_event=False,
    broker_connection_retry_on_startup=True,
)
if os.environ.get("CM_HELD_KEYPREFIX"):
    # kombu's Redis `global_keyprefix`: every key, `unacked` included, gets it.
    app.conf.broker_transport_options = {"global_keyprefix": os.environ["CM_HELD_KEYPREFIX"]}


@app.task(name="held.sleepy", acks_late=True)
def sleepy(seconds: float, release_key: str | None = None) -> str:
    """Sleep up to `seconds`, or until `release_key` exists in the result
    backend's Redis. `acks_late` keeps the message unacknowledged for the
    whole run, which is what a `kill -9` mid-task leaves behind."""
    deadline = time.monotonic() + seconds
    client = app.backend.client if release_key and hasattr(app.backend, "client") else None
    while time.monotonic() < deadline:
        if client is not None and client.exists(release_key):
            break
        time.sleep(0.1)
    return "done"


@app.task(name="held.noop")
def noop(*args, **kwargs) -> str:
    return "noop"
