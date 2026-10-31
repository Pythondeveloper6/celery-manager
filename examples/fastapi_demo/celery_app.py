import os

# Workaround for an upstream Celery 5.6 / billiard 4.3 bug on macOS: billiard
# spawns (not forks) pool children there, and Celery only initialises a spawned
# child when this is set; without it every task fails with
# "ValueError: not enough values to unpack (expected 3, got 0)".
os.environ.setdefault("FORKED_BY_MULTIPROCESSING", "1")

from datetime import timedelta

import settings
from celery import Celery
from celery.schedules import schedule

app = Celery("demo", broker=settings.BROKER_URL, backend=settings.RESULT_BACKEND, include=["tasks"])
app.conf.task_always_eager = settings.EAGER
# FastAPI runs sync routes in worker threads; make this the app those threads
# fall back to, otherwise Celery would use its default amqp:// app there.
app.set_default()
# The events listener only sees what Celery emits: workers must send task events
# and the publisher must send task-sent (worker/queue are null otherwise).
app.conf.worker_send_task_events = True
app.conf.task_send_sent_event = True
app.conf.beat_schedule = {
    "send-demo-report-every-2-hours": {
        "task": "tasks.send_report",
        "schedule": schedule(run_every=timedelta(hours=2)),
        "args": (),
    },
}
