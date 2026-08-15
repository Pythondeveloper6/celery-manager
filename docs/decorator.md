# The `@track` decorator

Apply `celery_manager.decorator.track()` above `@app.task` to opt a task
into two things:

1. **Per-occurrence schedule control** — if someone pauses/cancels a
   specific future run from the dashboard, this decorator is what makes
   the task actually skip its body at that run instead of executing.
2. **Full log capture** — stdout/stderr and `logging` calls made during
   the task are captured and shown on the task's detail page, not just the
   traceback on failure.

```python
from celery_manager.decorator import track

@app.task(bind=True)
@track(schedule_name="send-report-every-2-hours")
def send_report(self):
    ...
```

`schedule_name` should match the name you use for this schedule in the
dashboard's periodic schedules (i.e. the key under `beat_schedule` in your
Celery app's config). If omitted, it defaults to the task's registered
name — fine as long as you only have one schedule per task.

Tasks *without* `@track` are still visible in the dashboard (the events
listener picks up every task automatically) but don't support
per-occurrence pause/cancel or full log capture — only the traceback
Celery already provides on failure.

## Wiring

`@track` needs a session factory before it can do anything, which
`create_app()` sets up for you automatically (it calls
`celery_manager.decorator.configure(session_factory,
eta_tolerance_seconds=config.eta_tolerance_seconds)` during app startup).
If a decorated task somehow runs in a process that never called
`create_app()` or `configure()` directly, `@track` degrades gracefully: it
runs the task body normally, with no log capture and no schedule-override
check, rather than raising.

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

`reschedule` is a recognized `action` value on `ScheduleOverride` (and
appears in the API/dashboard types), but the decorator's skip check only
special-cases `skip` and `cancel` — a `reschedule` override does not
currently change what the decorator does when the task's original
occurrence comes around.
