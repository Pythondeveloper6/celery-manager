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

## Missed runs after an edit

[Missed-run detection](decorator.md#missed-run-detection) checks the
edited schedule, and only from the time it was saved. Runs before that
followed the old timing, which is no longer stored, so they aren't
checked. Revert to code removes that saved time, so the window the edit
covered is checked again against the code's schedule. It may then report
runs that beat never meant to send.

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

## Maintenance windows

A maintenance window skips chosen schedules on a weekly recurrence, for
example every Sunday from 02:00 to 04:00, and ends by itself: nothing is
paused, so there is nothing to resume. Beat keeps sending; each firing due
inside an enabled window is recorded as `skipped` and the task body does not
run, exactly like a [pause](rest-api.md#pausing-a-whole-schedule). The run's
detail shows why: `Skipped: maintenance window: Sunday patching`.

Manage windows on the **Schedules** page, under the table. Anyone signed in can
see them; operators can create, edit and delete them (each change is in the
audit log). A window has:

- **Days** of the week, a **start time** and a **duration**. It can cross
  midnight (Saturday 23:00 for 3 hours covers early Sunday), and can last up
  to a week.
- A **timezone**, an IANA name such as `Europe/Berlin`. It defaults to the
  Celery app's `timezone` setting, or `UTC` if that is unset.
- **Applies to**: all schedules, or the ones you pick.
- **Enabled**: a disabled window is kept but ignored.

The dialog previews the next five windows as you type.

How a firing is judged:

- By the time it was *due*, not when a worker picked it up. With beat running
  `celery_manager.beat:Scheduler` that is the stamped occurrence time; without
  it, the time the task starts.
- The start is inclusive and the end exclusive, so a run due exactly at 04:00
  runs under a window that ends at 04:00.
- Windows follow their timezone's daylight saving changes. The duration is real
  elapsed time, so a window that spans a clock change is an hour longer or
  shorter on the wall clock. A start time that does not exist on a
  spring-forward day (02:30 when the clocks jump from 02:00 to 03:00) begins
  when the clocks reach it; an ambiguous one on a fall-back day uses the first.
- Edits and deletions apply from the next firing. Firings already skipped stay
  skipped; nothing is caught up afterwards.
- If the schedule is also paused, or the occurrence was skipped by an operator,
  that reason is the one recorded.

Upcoming runs on a schedule's page are marked **Maintenance window: name**
before they happen, and the schedule list shows a chip with the window's name
(highlighted while it is in progress). [Missed-run
detection](decorator.md#missed-run-detection) is unaffected: a run skipped by a
window counts as arrived, because the decorator still records it.

The decorator must be configured in the worker process (`celery_manager.setup`),
as for pausing; windows are read from the same database.

```python
from celery_manager.control.maintenance import active_windows, create_window

with session_factory() as session:
    create_window(session, {
        "name": "Sunday patching", "days_of_week": [6], "start_time": "02:00",
        "duration_minutes": 120, "timezone": "Europe/Berlin", "applies_to": "*",
    }, "alice")
    active_windows(session, "nightly", datetime.now(timezone.utc))
```


## Dashboard definition controls

On **Schedules**, administrators using a writable scheduler backend can choose
**New schedule**, enter its unique name and registered task name, then describe
when it runs. The description is parsed by the deterministic grammar described
above. Enter an optional IANA timezone such as `Europe/Berlin` for calendar
schedules; leave it blank to use the app timezone. Intervals are elapsed durations
and do not need a timezone. **Preview next runs** returns the normalized definition
and renders occurrences in its reported timezone. Changing the description or
zone invalidates that preview, so creation always uses the reviewed definition.

Creation accepts positional arguments as a JSON array, keyword arguments as a JSON
object, and delivery options as a JSON object. For example, arguments `[42]`,
keyword arguments `{"format":"csv"}`, and options `{"queue":"reports","priority":5}`.
Supported delivery options are `queue`, `exchange`, `routing_key`, `priority`,
`expires`, and `headers`; the server rejects unsupported keys and reserved
`cm_` headers. These values are part of the schedule definition and affect future
scheduled messages.

Operators can expand **Edit arguments and delivery options** on a writable
schedule. Saving replaces those three complete fields; omitted old keys are
removed. Existing runs retain their recorded inputs. The normal **Edit schedule**
sheet supports an explicit timezone on crontabs as well as interval and cron
fields. Preview errors block saving. Static code-owned backends remain read-only
for argument and delivery-option changes.

Administrators also see **Schedule backup and reconciliation**. This provides
export, adoption, and repair with a reviewed before/after plan and explicit apply
approval. See [durable SQL schedules and ownership](sql-schedules.md) for the
workflow, supported schedules, and concurrency limits.
