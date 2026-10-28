# Editing schedules

Operators can change when a periodic task runs from the dashboard: switch
between a crontab and a fixed interval, change the fields, and see the next
five runs before saving. The schedule in your code stays the default, and
**Revert to code** goes back to it.

Beat has to apply the edit, so this needs two things:

1. Beat runs celery-manager's database-backed scheduler.
2. The dashboard is told that it does (`schedule_editing=True`).

Until both are done, the editor can only preview.

## 1. Run beat with `DatabaseScheduler`

```python
# Where you configure Celery: the same URL as CeleryManagerConfig.db_url.
app.conf.celery_manager_db_url = "postgresql://user:pass@db/celery_manager"
```

```bash
celery -A yourapp beat -S celery_manager.beat:DatabaseScheduler
```

(or `app.conf.beat_scheduler = "celery_manager.beat:DatabaseScheduler"`).

`DatabaseScheduler` is the usual `celery_manager.beat:Scheduler` (Celery's
`PersistentScheduler` that stamps each run's occurrence) plus one thing:
it reads the saved edits from celery-manager's database.

- **When.** At startup, then every `app.conf.celery_manager_schedule_refresh`
  seconds (default `10`). Each read is one small query. Beat never sleeps
  past the next read, so an hourly task edited to run every minute doesn't
  wait out the hour first. No restart is needed.
- **What changes.** Only the entry's timing. The task, arguments and
  options still come from `beat_schedule`. An entry that is not in
  `beat_schedule` (Celery's own `celery.backend_cleanup`) is left alone.
- **What is kept.** Beat's `last_run_at` for the entry. An interval edit
  fires at "last run + new interval". A crontab edit fires at its next
  matching time.
- **Restarts.** Celery resets every entry to the code's timing when beat
  starts, and the scheduler then applies the edits from the database again.
  The database is the only place an edit is stored.
- **Database down.** Beat logs the error and keeps firing with the
  schedules it already has. It tries again at the next refresh.
- **Read-only.** Beat never writes to the database and never migrates it.
  Start the API (or run `celery-manager migrate`) first, so the
  `periodic_schedules.schedule_override` column exists.

Running beat with `celery_manager.beat:Scheduler` doesn't need the
database at all, and it still works exactly as before. It just never sees
an edit.

!!! note "django-celery-beat, RedBeat and other schedulers"
    `DatabaseScheduler` builds on Celery's `PersistentScheduler`, so it can't
    be combined with another scheduler that keeps its own schedules.
    django-celery-beat stores schedules in Django's tables. Edit them in the
    Django admin, and leave `schedule_editing` off. You can still use
    `StampOccurrencesMixin` with it for occurrence stamping (see
    [the decorator](decorator.md#how-the-skipcancel-check-works)).

## 2. Turn editing on in the API

```python
config = CeleryManagerConfig(
    celery_app=app,
    db_url="postgresql://user:pass@db/celery_manager",
    secret_key=...,
    schedule_editing=True,
)
```

It defaults to `False`. celery-manager can't see which scheduler beat is
running. If editing were on with the wrong scheduler, the dashboard would
list runs that never happen. While it's off:

- `PUT /api/schedules/{name}/schedule` answers `409`, and its message says why.
- The editor opens for previewing and reverting only, with Save disabled
  and the reason shown.
- An edit saved earlier is listed (marked "not applied") but not used for
  upcoming runs, because beat isn't applying it. **Revert to code** still
  clears it.

## Using the editor

Open a schedule and click **Edit schedule** (operators and admins only).

- **On a crontab**: minute, hour, day of month, month, day of week, in
  Celery's syntax (`*`, `0`, `*/15`, `1-5`, `mon-fri`). Fields are checked
  by Celery's own `crontab(...)` parser. A crontab that can never fire
  (31 February) is refused. Times are in the Celery app's `timezone`, like
  the crontabs in `beat_schedule`.
- **Every fixed interval**: a number of seconds, minutes, hours or days.
  The minimum is 1 second and the maximum is 366 days.
- **Next five runs** updates as you type. The server computes them the
  same way it will list them after you save. An invalid value shows
  Celery's reason instead.
- **Save schedule** stores the edit. The schedule page and list then show
  the edited timing with "edited by *user*, *time*".
- **Revert to code** removes the edit. The schedule then runs as
  `beat_schedule` says.

Both actions are written to the audit log as `edit_schedule` (before,
after, the code's schedule) and `revert_schedule`.

## Skipped and cancelled runs after an edit

A skip or cancel targets one run time. If the new schedule has no run at
that time, the skip is **kept, not deleted**:

- It never matches while the edit is in force. Beat stamps each message
  with the run time it was due at, and the worker only applies a skip to
  that exact run, so a kept skip can't hit some other run.
- If you revert, or edit the schedule back so that run exists again, the
  skip applies again.
- The save response lists these as `unmatched_overrides`. The dashboard
  says how many there are, and the audit row records their times.

Deleting them would quietly undo another operator's decision, so they are
kept.

## Python API

The same operations are in `celery_manager.control`:

```python
from celery_manager.control import edit_schedule, preview_schedule, revert_schedule

with session_factory() as session:
    preview_schedule(session, celery_app, "nightly", {"type": "crontab", "minute": "30", "hour": "2"})
    row, unmatched = edit_schedule(session, celery_app, "nightly", {"type": "interval", "seconds": 900}, "alice")
    revert_schedule(session, "nightly", "alice")
```

An invalid spec raises `celery_manager.schedule_specs.ScheduleSpecError`.
For the schedule in force (edit or code), use
`celery_manager.control.schedules.effective_schedule(session, name, entry)`.
