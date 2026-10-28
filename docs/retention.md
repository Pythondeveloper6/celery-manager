---
title: Data retention
---

# Data retention

Every task run is stored, with its arguments, traceback and captured logs.
On a busy system that grows without limit. Data retention deletes runs older
than a number of days you choose, per task if you like.

**It is off by default.** Until an admin sets a number of days, nothing is
ever deleted, so upgrading changes nothing.

## Setting the rules

In the dashboard, an admin opens **Settings**, then **Data retention**:

- **Keep runs for (days)** is the default for every task. Empty keeps runs
  forever.
- **Keep failed runs for (days)** is optional. It can only keep failed runs
  *longer* than the rule that applies to their task, so it must be at least
  the default. Empty means failed runs follow the normal rule.
- **Keep the audit log for (days)** also deletes old [audit log](dashboard.md#audit-log)
  entries. Empty, the default, keeps them all.
- **Per-task rules** override the default for one task. Pick the task from
  the list of tasks celery-manager knows, then give a number of days, or tick
  **Keep forever** to exempt that task from the default.

**Preview what would be deleted** counts, without deleting anything, what a
prune would remove if the form were saved. Anyone signed in can preview;
saving needs the `admin` [role](roles.md).

The rules are stored in the `app_settings` table, so there is no migration
for this feature.

From Python:

```python
from celery_manager.control.retention import update_retention

update_retention(session, {
    "default_days": 30,
    "failed_days": 90,
    "tasks": {"reports.nightly": 7, "billing.invoice": None},  # None: keep forever
}, username="admin")
```

## What is deleted, and what never is

A run's age is when it **finished**. A run that never finished (the worker
died, an event was lost) is judged by when it was last updated instead.

Never deleted:

- A **queued or started** run is only deleted after it has gone the whole
  period with no update. A run that is waiting or still working is safe.
  Progress reports count as an update.
- A run with an **ETA in the future**, however old its row is.
- With failed runs kept longer, a **retry of a failed run** is kept for as
  long as the failed run is. Otherwise the original would show up again under
  [Dead letters](dead-letters.md) once its retry was deleted.

Deleting a run also deletes its captured logs and progress, which live on the
same row. Retry and workflow links store task ids rather than database
references, so a run whose parent was pruned still displays normally; the
parent is simply not shown.

The [Insights](insights.md) pages and [metrics](metrics.md) are computed from
stored runs, so history shorter than your retention period disappears from
them as well.

## Queue depth samples

The [depth history](queues.md#depth-history) samples are not governed by
these rules. They have no task name and are tiny, so one setting covers
them: `queue_sample_retention_days` (14 by default, `0` keeps them). A prune
deletes older samples along with the runs and audit rows, and reports them as
`samples_deleted`.

## Running a prune

Nothing prunes on its own. Pick one of these.

**From the command line** (use cron or a Kubernetes CronJob):

```bash
celery-manager prune --db-url postgresql://... --dry-run   # see what would go
celery-manager prune --db-url postgresql://...             # do it
```

`--batch-size` (default 1000) sets how many rows each transaction deletes.

**From Celery beat**, with the optional built-in task:

```python
from celery_manager.tasks import register_prune_task

register_prune_task(app, config, schedule=3600.0)   # hourly
```

Put it in the module your workers import. It registers the task
`celery_manager.tasks.prune` and, because `schedule` is given, a beat entry
named `celery-manager-prune`. Leave `schedule` out to add the entry yourself
or to call the task by hand. Nothing is registered unless you call it.

Large first prunes are safe: rows go in batches, one short transaction per
batch, so the table is never locked for the whole run.

## What gets recorded

A prune that deleted something writes one [audit log](dashboard.md#audit-log)
row, `prune` on `retention`, with the counts (the 25 busiest tasks, if more
were touched). The user is `cli` for the command line and `system` for the
Celery task. A prune that finds nothing writes no row, so an hourly schedule
does not fill the log. Changing the rules is audited as an `update` on
`retention`. The **Last prune** line on the Settings page shows the most
recent real prune.
