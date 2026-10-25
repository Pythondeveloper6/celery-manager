# The `@track` decorator

Apply `celery_manager.decorator.track()` above `@app.task` to opt a task
into three things:

1. **Per-occurrence schedule control** — if someone pauses/cancels a
   specific future run from the dashboard, this decorator is what makes
   the task actually skip its body at that run instead of executing.
2. **Log capture** — `logging` output produced during the task is captured
   and shown on the task's detail page, not just the traceback on failure.
   See [What "log capture" actually captures](#what-log-capture-actually-captures)
   — `print()` is included in a real worker, but for a reason that is worth
   knowing.
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

`bind=True` is **not** required. It is used throughout these docs because a
bound task is what lets you call `self.retry()`, and because it is the more
common shape for the kind of task worth tracking — but `@track` works on an
ordinary unbound task too, and records it under its real name and task id:

```python
@app.task                       # no bind=True
@celery_manager.track()
def send_welcome_email(address):
    ...
```

Under the hood the decorator identifies the bound `Task` by type and falls
back to Celery's `current_task` when there isn't one, rather than assuming the
first positional argument is the task. That distinction matters: assuming it
meant an unbound task's first argument was silently treated as the task
object, which is how this used to fail.

This decorator only does its job in a process that has been wired with
`celery_manager.setup(config)` — see [Wiring](#wiring--the-worker-process-must-call-celery_managersetup)
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

!!! note "What a retry attempt clears"
    Celery reuses the task id across retry attempts, so there is one row for
    all of them. Attempt N+1's `started` write rewrites the status (correct
    — the row should show the attempt now in flight) and additionally
    **erases** what the previous attempt left behind: `exception`,
    `traceback`, `finished_at`, `runtime_seconds`, and all four `progress_*`
    columns. Without that, a task that failed once and then succeeded showed
    as `succeeded` with a full traceback and a stale progress bar attached,
    which is a worse lie than showing nothing. `log_output` is *not* cleared
    here — it is rewritten wholesale by whichever branch ends the attempt.

**Cost: one extra INSERT/UPDATE and commit per tracked run.** That is
accepted deliberately — it is one write against a run that is about to do
real work, and it buys visibility of every in-flight task without requiring
the events listener at all. Like every other write in this decorator, a
failure is logged and dropped: it can never change your task's outcome.

### `self.retry()` is recorded as `retried`, not `failed`

`self.retry()` raises `celery.exceptions.Retry`, which is an ordinary
`Exception` subclass — so it used to land in the decorator's failure branch
and record a run Celery considers RETRY as `failed`, with Celery's
`"Retry in 30s: RuntimeError('boom')"` message sitting in the exception
column and a finish time on a run that has not finished. It is now recorded
as `status="retried"` with `finished_at`/`runtime_seconds` left unset,
matching the `retried` status the events listener writes from
`task-retried`; the `Retry` is re-raised unchanged, so Celery's own retry
machinery is untouched.

Only `Retry` is special-cased. `Ignore` and `Reject` share its base class but
mean "this message is being dropped", which has no status in
celery-manager's vocabulary — they still fall through to `failed`, which at
least surfaces them rather than silently reclassifying them.

### Args and kwargs are stored best-effort

The decorator serializes a run's args/kwargs to JSON *before* calling your
task body, which makes the serializer a hazard: `json.dumps` is stricter than
Celery is. Celery's own JSON serializer round-trips `datetime`, `date`,
`UUID` and `Decimal`, so `send_task(kwargs={"since": datetime(...)})` is
ordinary supported usage that arrives in the worker as a real `datetime` —
and stdlib `json.dumps` raises `TypeError` on it. That exception used to
propagate out of the wrapper, marking the task FAILURE without ever running
its body: celery-manager deciding a task's outcome, which is exactly what it
must never do.

Values stdlib JSON cannot express are now stored as their `repr` inside
otherwise-valid JSON, so the shape survives (a list stays a list) and the
value stays searchable by `q=`. The trade-off: those leaves no longer
round-trip, so [retrying such a run](rest-api.md#retrying-a-run-the-listener-recorded)
re-dispatches the repr string rather than the original object. Pass explicit
`args`/`kwargs` to `control.retry_task()` when that matters.

## What "log capture" actually captures

`capture_logs()` attaches a `logging.StreamHandler` to the **root logger**
for the duration of the task body, and nothing else. It does not redirect
`sys.stdout` or `sys.stderr`. So what lands in `log_output` is: anything
logged through the `logging` module by your task or by any library it calls,
as long as that logger propagates to root.

`print()` is captured too in a real Celery worker — but indirectly, and it is
worth knowing why, because it stops being true in some setups. Celery's
worker sets `worker_redirect_stdouts = True` by default, which replaces
`sys.stdout`/`sys.stderr` with a proxy that writes to the `celery.redirected`
logger at `WARNING`. That propagates to root, so our handler sees it:

```
2026-08-20 12:20:59,343 WARNING celery.redirected: PRINTED-IN-WORKER
```

Two consequences:

- **`print()` is not captured outside a worker.** Under
  `task_always_eager = True` (a test, a shell, `.apply()`), nobody installs
  that redirect, so `print()` goes to the real stdout and `log_output` holds
  only the `logging` calls.
- **`print()` is not captured if you turn the redirect off.** Setting
  `worker_redirect_stdouts = False`, or raising
  `worker_redirect_stdouts_level` above the level your prints land at, takes
  it away — and celery-manager has no way to notice.

If you want output that is reliably captured in every one of those setups,
log it: `logger.info(...)` rather than `print(...)`.

!!! warning "Empty `log_output`? Check your log level."
    `capture_logs()` adds a handler but never sets a level, so the **root
    logger's** effective level still decides whether a record is created at
    all. Python's default is `WARNING`, which means a task doing
    `logger.info("...")` produces an empty `log_output` unless something
    raised the level — and a Celery worker only does that if you started it
    with a level that allows it (`celery -A app worker -l info`). Started at
    the default `WARNING`, `logger.info()` from inside your task is dropped
    before celery-manager ever sees it.

    This bites in eager mode too: running the bundled demo's tasks through
    `.apply()` in a plain shell records the run correctly but leaves
    `log_output` empty, because nothing configured logging in that process.

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
- Otherwise the task runs normally. Its log output is captured (via
  `celery_manager.logcapture.capture_logs`, which attaches a handler to the
  root logger for the duration of the call) and, together with the outcome
  (`succeeded`/`failed`), exception, traceback, and runtime, persisted as a
  `TaskRun` row.

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
