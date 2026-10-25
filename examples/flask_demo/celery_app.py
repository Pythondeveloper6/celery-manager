from datetime import timedelta

import settings
from celery import Celery
from celery.schedules import schedule

app = Celery("demo", broker=settings.BROKER_URL, backend=settings.RESULT_BACKEND, include=["tasks"])
app.conf.task_always_eager = settings.EAGER
app.conf.beat_schedule = {
    "send-demo-report-every-2-hours": {
        "task": "tasks.send_report",
        "schedule": schedule(run_every=timedelta(hours=2)),
        "args": (),
    },
}
