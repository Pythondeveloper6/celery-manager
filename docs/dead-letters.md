---
title: Dead letters
---

# Dead letters

The **Dead letters** page lists runs that failed for good, in one place, and
lets you fix their arguments and run them again, one at a time or many at once.

## What counts as a dead letter

A run is a dead letter when both of these are true:

- Its status is `failed`. While Celery is still retrying a task the run reads
  `retried`; it only becomes `failed` once Celery has given up, so every
  retry a task was allowed has been used.
- Nothing was run again from it. When you run a dead letter again (here, or
  with **Retry** on its page) the new run records which run it came from, and
  the original leaves the list straight away, whether the new run succeeds,
  is still queued, or fails again.

A new run that fails again is a dead letter in its own right. Its **Earlier
runs** column counts the runs that came before it in the chain, so a task that
keeps failing is easy to spot.

Nothing extra is stored: the list is a query over the run history, so there
is no new table and no migration. Celery's own retries reuse the task id and
are not separate rows, so **Earlier runs** counts re-runs made through
celery-manager, not Celery's automatic retries.

## The page

- **Summary.** Dead letters counted by task and exception type, biggest first.
  Click a group to filter the list to it.
- **Filters.** Task, exception type, and how recently the run failed.
- **The list.** Each row shows the task, the exception type, the last line of
  the traceback, how many earlier runs led to it, and when it failed. The task
  links to the run's own page.

Viewers see the page and the list but not the buttons.

## Running them again

Operators (see [Roles](roles.md)) can:

- **Edit arguments and run again** on a row: opens the editor prefilled with
  the run's arguments and keyword arguments. The new run goes to the same
  queue as the original.
- **Run N again, same arguments** for the selected rows. Tasks can be mixed.
- **Run again with new arguments** for the selected rows: one set of
  arguments for all of them, so every selected run must be of the same task.
  The button is disabled otherwise.

At most 500 runs can be run again in one go. Selection survives paging and
filtering, so you can pick runs across pages.

Everything is checked before anything is sent. If a selected run is no longer
a dead letter (someone else ran it again first), or its stored arguments cannot
be read back, nothing is dispatched and the message says why. For stored
arguments that cannot be read, supply them yourself with the editor. If the
broker refuses one run part-way, the others still go, and the refused ones stay
in the list.

After a run the page shows links to the new runs. Each run is written to the
[audit log](rest-api.md#audit-log) as `rerun_dead_letter`, with whether the
arguments were changed (and the arguments sent, when they were).

## Not included

There is no "dismiss" for a dead letter you don't intend to run again.
Remembering that choice needs a column to keep it in, which means a database
migration; it is left out until then.

## REST API

| Method | Path | Role |
|---|---|---|
| `GET` | `/api/dead-letters` | any signed-in user |
| `POST` | `/api/dead-letters/rerun` | `operator` |

See the [REST API reference](rest-api.md#dead-letters).
