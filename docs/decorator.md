# The `@track` decorator

Apply `celery_manager.decorator.track()` above `@app.task` to opt a task
into three things:

1. **Per-occurrence schedule control** — if someone pauses/cancels a
   specific future run from the dashboard, this decorator is what makes
   the task actually skip its body at that run instead of executing.
2. **Full log capture** — stdout/stderr and `logging` calls made during
   the task are captured and shown on the task's detail page, not just the
   traceback on failure.
3. **Visibility while the task is still running** — the decorator records a
   `started` row *before* it calls your function, so an in-flight task is
   visible even with no events-listener process running, and
   [`report_progress()`](progress.md) has a row to write to. See
   [The `started` row](#the-started-row-written-before-your-task-body).

```python
import celery_manager

@app.task(bind=True)
@celery_manager.track(schedule_name="send-report-every-2-hours")
def send_report(self):
    ...
```

(`from celery_manager.decorator import track` also works and is equivalent —
`celery_manager.track` is just the documented public alias.)

This decorator only does its job in a process that has been wired with
`celery_manager.setup(config)` — see [Wiring](#wiring-the-worker-process-must-call-celery_managersetup)
below, which is the step most easily missed.

`schedule_name` should match the name you use for this schedule in the
dashboard's periodic schedules (i.e. the key under `beat_schedule` in your
Celery app's config). If omitted, it defaults to the task's registered
name — fine as long as you only have one schedule per task.

Tasks *without* `@track` are still visible in the dashboard (the events
listener picks up every task automatically) but don't support
per-occurrence pause/cancel or full log capture — only the traceback
Celery already provides on failure. ([`report_progress()`](progress.md) is
the exception: it works on an undecorated task too, via
`celery.current_task`.)

## Wiring — the worker process must call `celery_manager.setup()`

`@track` needs a session factory before it can do anything, and that wiring
is **per process**. Decorated tasks execute in your **Celery worker**
process, so the worker is the process that has to be wired — not (only) the
one that builds the dashboard app.

**Call `celery_manager.setup(config)` at import time in the module that
defines your tasks**, so it runs before any task does:

```python
import celery_manager
from celery_manager.config import CeleryManagerConfig

from myproject.celery import app

celery_manager.setup(CeleryManagerConfig(
    celery_app=app,
    db_url="sqlite:///celery_manager.db",  # the same DB the dashboard reads
    secret_key="a-real-secret-in-production",
))


@app.task(bind=True)
@celery_manager.track(schedule_name="send-report-every-2-hours")
def send_report(self):
    ...
```

`setup()` does the whole per-process wiring (create the engine, ensure the
tables exist, hand the decorator a session factory) and is idempotent, so
it's safe if more than one module calls it. `create_app()` calls the same
function internally, which is why the dashboard process doesn't need a
separate `setup()` call — but the dashboard process is *not* where your
tasks run, so wiring it does nothing for the worker.

If a decorated task runs in a process that never called `setup()`, `@track`
degrades gracefully rather than raising: the task body still runs normally,
but **with no log capture, no schedule-override check, and no `task_runs`
row of its own** — meaning per-occurrence pause/cancel silently does nothing
and `report_progress()` has nowhere to write. Because that failure is
invisible from the outside, the decorator logs a one-time `WARNING`
(`celery_manager.decorator`) the first time a tracked task runs unconfigured.
If you're not seeing pause/skip or captured logs, check the worker's log for
that warning first.

## The `started` row written before your task body

Before calling your function, `@track` writes (or upserts) a `task_runs` row
with `status="started"`, the task name, its args/kwargs, the schedule name,
and `started_at`. The terminal `succeeded`/`failed` row is written afterwards
as before.

This exists because the decorator used to write *only* when a task finished.
A long-running task was therefore invisible in the dashboard unless the
separate events-listener process happened to be running, and
[`report_progress()`](progress.md) had no row to write to until the run was
already over — which defeats live progress entirely.

What makes it safe:

- **Against itself.** The `started` write and the terminal write happen in
  the same thread, in sequence, around the body. The final
  `succeeded`/`failed` always lands after the `started` and therefore wins.
  There is no "late started" window inside the decorator.
- **Against the events listener.** The listener treats `started` as an
  ordinary lifecycle status it may overwrite — only `skipped` is in its
  decorator-owned guard — and every event it could apply here
  (`task-started`, `task-succeeded`, `task-failed`) is emitted by the worker
  at or after the moment this row is written. `started` is deliberately
  *not* added to that guard: doing so would stop the listener applying the
  real terminal status to a run whose decorator write failed, which is
  strictly worse.

!!! note "One retry wrinkle, accepted rather than fixed"
    Celery reuses the task id across retry attempts. So attempt N+1 rewrites
    `started` over the listener's `retried` — correct, the row should show
    the attempt now in flight — but the *previous* attempt's `finished_at`,
    `runtime_seconds`, and `progress_*` values stay on the row, because the
    upsert skips `None` values and therefore cannot clear a field. The row
    self-corrects the moment the new attempt finishes. Until then, a
    retrying task can briefly show a stale runtime.

**Cost: one extra INSERT/UPDATE and commit per tracked run.** That is
accepted deliberately — it is one write against a run that is about to do
real work, and it buys visibility of every in-flight task without requiring
the events listener at all. Like every other write in this decorator, a
failure is logged and dropped: it can never change your task's outcome.

## How the skip/cancel check works

On every call, the wrapper looks for an unconsumed `ScheduleOverride` row
for `effective_schedule_name` (either the given `schedule_name` or the
task's registered name) whose `target_time` is within
`eta_tolerance_seconds` (default 60, configurable via
[`CeleryManagerConfig.eta_tolerance_seconds`](configuration.md)) of *now*.
This tolerance-window match — not exact timestamp equality — is
deliberate: a periodic task's actual fire time and the `target_time` a user
paused from the dashboard's occurrence list are computed independently and
will rarely line up to the microsecond.

- If a matching override exists and its `action` is `skip` or `cancel`,
  the decorator marks it `consumed`, records a `TaskRun` with
  `status="skipped"`, and returns `None` **without calling the wrapped
  function** — the task body never runs for that occurrence.
- Otherwise the task runs normally. Its stdout/logging output is captured
  (via `celery_manager.logcapture.capture_logs`, which attaches a handler
  to the root logger for the duration of the call) and, together with the
  outcome (`succeeded`/`failed`), exception, traceback, and runtime,
  persisted as a `TaskRun` row.

One implementation detail worth knowing if you inspect the database
directly: SQLite drops timezone info on `DateTime(timezone=True)` columns
on read-back, even though every value in this codebase is always written
as UTC. The matching logic treats any timezone-naive `target_time` read
from the database as UTC rather than as local time — this only matters if
you're querying `schedule_overrides` yourself; the decorator and API
handle it transparently.

`reschedule` exists as a value the `ScheduleOverride.action` column permits
(alongside the unused `reschedule_to` column), but the decorator's skip check
only special-cases `skip` and `cancel` — a `reschedule` override would not
change what the decorator does when the task's original occurrence comes
around. Because of that, the pause endpoint now rejects `action="reschedule"`
with a `422` rather than storing an override that does nothing while
displaying as paused.
