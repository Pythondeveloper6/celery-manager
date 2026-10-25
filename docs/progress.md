# Live progress

Celery's event stream carries lifecycle transitions — sent, started,
succeeded, failed — and nothing else. It cannot tell the dashboard that a
task is on row 412 of 10,000, because only the task itself knows that. So
celery-manager gives you one function to say it:

```python
from celery_manager import report_progress

report_progress(current, total=None, message=None)
```

Call it from *inside* your task body, as often as is natural. It writes to
the `task_runs` row for the run that is currently executing, so the number
shows up while the task is still in flight.

`celery_manager.progress.report_progress` is the same function —
`celery_manager.report_progress` is the documented public alias.

## A basic loop

```python
import celery_manager
from celery_manager import report_progress


@app.task(bind=True)
@celery_manager.track()
def import_customers(self, row_count: int = 60):
    for row in range(1, row_count + 1):
        handle_row(row)
        report_progress(row, row_count, f"row {row} of {row_count}")
    return f"imported {row_count} rows"
```

That is the whole API. There is nothing to initialize, no context object to
thread through your helpers, and no state to clean up — `@track` binds the
current run for the duration of the task body and unbinds it afterwards.

The same task exists, runnable, in the bundled Django demo as
`tasks_app.tasks.import_customers`.

## Open-ended progress: no total

Some work genuinely has no denominator — draining a queue, following a
stream, scanning until exhausted. Omit `total`:

```python
@app.task(bind=True)
@celery_manager.track()
def tail_event_stream(self, seconds: int = 30):
    seen = 0
    for _ in range(seconds):
        seen += poll_once()
        report_progress(seen, message=f"{seen} events seen")
```

`progress_total` is then stored as `null`, and there is no percentage to
compute. The dashboard shows the running count and the message rather than
inventing a progress bar it cannot honestly fill.

A `total` of `0`, a negative number, or anything that isn't int-able is
treated exactly like `None` — open-ended. celery-manager never invents a
denominator.

## The write throttle

`report_progress()` is meant to be safe inside a tight loop, and a naive
implementation inside a 100,000-row loop is 100,000 commits. So actual
database writes are throttled to **at most one per 0.5 seconds per run**;
calls that arrive inside that window are dropped, not queued.

The throttle interval is the module-level constant
`celery_manager.progress.MIN_WRITE_INTERVAL_SECONDS`. It is a plain module
attribute, **not** a field on
[`CeleryManagerConfig`](configuration.md) — an operator with an unusually
loaded database can raise it by assigning to it before tasks start:

```python
from celery_manager import progress

progress.MIN_WRITE_INTERVAL_SECONDS = 2.0
```

Two consequences worth knowing:

- **The completing call always writes.** A call where `total` is known and
  `current >= total` bypasses the throttle, because the final
  `10000 / 10000` is the update people actually look for and dropping it
  would leave every finished task displaying 99%.
- **An open-ended loop's last update can be lost.** The throttle is a
  pre-write check that never queues anything, and a `total=None` loop has no
  "completing" call to force through, so the very last
  `report_progress(seen)` before the task returns may simply be dropped. The
  row then shows a count that is a fraction of a second stale, forever. This
  is accepted: the run's terminal status is what tells you it finished.

The throttle uses a monotonic clock, so an NTP step or a DST change on a
misconfigured host can neither freeze progress for hours nor defeat the
throttle.

## Messages are sticky

Passing `message=None` — the default — leaves whatever message is already on
the row **untouched**. That is deliberate: the common shape is a caller who
sets a phase label once and then just counts.

```python
report_progress(0, len(rows), "fetching")   # message set
for i, row in enumerate(rows):
    handle(row)
    report_progress(i + 1, len(rows))       # still says "fetching"
```

To clear a message, pass an empty string — `""` is written through and
renders as no message:

```python
report_progress(i + 1, len(rows), "")
```

Messages are a one-line UI label ("row 412", "compressing"), not a log line.
Anything longer than 500 characters
(`celery_manager.progress.MAX_MESSAGE_CHARS`) is truncated rather than
rejected — a clipped label is still useful, and rejecting it would throw away
the progress numbers along with it. A non-string is coerced with `str()`.

## It never raises into your task

celery-manager is an observability layer wrapped around someone else's work,
so a progress call must never change a task's outcome. `report_progress()`
swallows everything:

| Situation | What happens |
|---|---|
| No running task at all (called from a shell, a test, or a helper used both inside and outside a task) | Nothing recorded. One `WARNING` per process on the `celery_manager.progress` logger, then silence. |
| The process never called `celery_manager.setup(config)` | Nothing recorded, and a *separate* one-off `WARNING` that names `setup()` — this is the one you will hit on a misconfigured worker. |
| `current` is negative, or not convertible to `int` | The call is ignored silently. |
| `total` is `0`, negative, or not convertible to `int` | Treated as `None` (open-ended). |
| A database write fails | Logged by the decorator's `_persist_run`; the task's own result and exception are untouched. |

Both warnings are logged **once per process**, so a task looping a hundred
thousand times cannot flood your worker log.

!!! tip "If progress never appears, check the worker log first"
    The most common cause is the same one that silently disables `@track`:
    the **worker** process never called `celery_manager.setup(config)`. The
    dashboard process calling `create_app()` does not help — that is not
    where your tasks run. See
    [Wiring](decorator.md#wiring--the-worker-process-must-call-celery_managersetup).

## Where the progress goes

Four nullable columns on `task_runs` (`celery_manager.db.models`):

| Column | Meaning |
|---|---|
| `progress_current` | The count the task last reported. |
| `progress_total` | The denominator, or `null` for open-ended progress. |
| `progress_message` | The last message, or `null` if none was ever set. |
| `progress_updated_at` | When that write happened (UTC). |

All four are served raw — no percentage, no ETA — by both
[`GET /api/tasks`](rest-api.md#tasks) and `GET /api/tasks/{task_id}`, so the
client can decide how to render them in whatever space it has. Progress is
opt-in, so for tasks that never call `report_progress()` they are simply
`null`, which is different from `0`.

## Tasks without `@track`

`@track` is the normal path, and it is the one to prefer: it creates the
run's row up front (see
[the `started` row](decorator.md#the-started-row-written-before-your-task-body))
and knows the run's effective id even when Celery gave it none.

But `report_progress()` also works in an undecorated task. If there is no
`@track` context it falls back to `celery.current_task.request.id`, and
progress lands on whatever row exists for that uuid — or on a row it inserts
itself, seeded with the task name Celery reports and `status="started"`, if
nothing has created one yet (which is what happens when the events listener
isn't running either). Those seed values are only ever used on insert; they
can never restate a status the decorator or the listener has established.

The whole Celery lookup is guarded, so a task running outside a real Celery
context degrades to "no progress recorded", never to an exception.
