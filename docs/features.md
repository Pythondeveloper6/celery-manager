# Features

Everything celery-manager does, in plain words. Each feature says what it is
for, how you use it, and where to read the details.

Two pieces do most of the work, so it helps to know them first:

- **The events listener** (`celery-manager events`) watches Celery and
  records every task run. Most of what you see in the dashboard comes from
  it, with no change to your tasks.
- **The `@track` decorator** is optional, per task. Add it when you want
  log capture, live progress, or the ability to skip and pause scheduled
  runs. Without it, a scheduled task always runs.

## See what your tasks are doing

**Task list.** Every run of every task, newest first, with its status,
runtime and start time. Filter by status, search by task name, ID or
arguments, pick a date range, or group runs by task.
In the dashboard: **Tasks**. [Read more](dashboard.md#task-list)

**Task detail.** One run in full: arguments, worker, queue, timings, and for
a failure, the exception and traceback first. Retries link back to the run
they came from.
In the dashboard: click any run. [Read more](dashboard.md#task-detail)

**Log capture.** Log lines your task writes while it runs are saved with
that run and shown on its page. Needs `@track`.
[Read more](decorator.md#what-log-capture-actually-captures)

**Live progress.** Call `report_progress(current, total, message)` inside a
long task and watch a progress bar, elapsed time and an estimated finish
while it runs. Needs `@track`.
[Read more](progress.md)

**Workflows.** When a run is part of a chain, group or chord, its page shows
the whole workflow as a tree, so you can see which step failed.
In the dashboard: the **Workflow** card on a run's page.
[Read more](dashboard.md#workflow-card)

**Live updates.** The task list and run pages update by themselves within
about a second. The **Live** badge in the header shows the connection is up.
Nothing to set up. [Read more](dashboard.md#live-updates)

## Act on tasks

**Start a task.** Pick any registered task, fill in its arguments (defaults
are prefilled from the function), and optionally choose a queue or a delay.
In the dashboard: **Start task** on the task list.
[Read more](dashboard.md#start-task)

**Cancel or terminate.** Cancel stops a run that hasn't started yet.
Terminate also kills one that is already running.
In the dashboard: the buttons on a run's page.

**Retry.** Run a failed task again with the same arguments, or use
**Retry with changes** to edit them first. Select several failed runs to
retry them together.

**Bulk cancel.** Cancel the runs you've selected, or every waiting or
running task that matches your current filters. You see the count before
you confirm. [Read more](queues.md#bulk-cancel)

## Control periodic tasks (beat schedules)

All of these need `@track` on the task. celery-manager doesn't change your
beat schedule: beat still sends the task, and `@track` makes it skip.

**Skip one upcoming run.** Open a schedule to see its next runs, and skip or
cancel just one of them. The rest of the schedule carries on as normal.
This is what celery-manager was built for.
In the dashboard: **Schedules**, then a schedule's name.
[Read more](dashboard.md#schedule-occurrences-future-schedule-view)

**Pause a whole schedule.** Stop a schedule from running until you resume
it, or until a time you choose (1 hour, tomorrow 9:00, or a custom time).
Resuming picks up from now: it doesn't catch up on the runs it missed.
In the dashboard: **Pause schedule** on the Schedules page.
[Read more](dashboard.md#pausing-a-whole-schedule)

## Understand trends and failures

**Insights.** How many runs, how many succeeded, and how long they take
(median and p95), over the last hour, day, week or month.
In the dashboard: **Insights**. [Read more](insights.md)

**A page per task.** The same numbers and charts for one task, plus its
recent runs and most common errors.
In the dashboard: click a task name in Insights or on a run's page.
[Read more](insights.md#task-page)

**Grouped failures.** Failures with the same cause are grouped together,
even when the messages differ slightly (an ID or a number), so a flood of
errors shows up as one problem. Retry a whole group at once.
In the dashboard: **Insights**, then **Failures**.
[Read more](insights.md#failures)

## Run your workers and queues

**Workers.** Every worker that is online: what it's running now, how many
tasks it has processed, and which queues it reads from. From here you can
shut a worker down, add or remove a pool process, and start or stop
reading from a queue.
In the dashboard: **Workers**. [Read more](workers.md)

**Rate limits.** Change how often a task may start, on the running workers,
without a deploy. The change lasts until a worker restarts.
In the dashboard: the **Rate limits** table on the Workers page.
[Read more](workers.md#runtime-rate-limits)

**Queues.** How many messages are waiting in each queue right now, and how
many workers read from it.
In the dashboard: **Queues**. [Read more](queues.md)

**Purge a queue.** Delete every waiting message in one queue. You type the
queue name to confirm. [Read more](queues.md#purging-a-queue)

## Share and keep what you find

**Links to a view.** Your filters are part of the page address, so
**Copy link** gives a teammate exactly what you're looking at.

**Saved views.** Save a set of filters under a name and come back to it
later. Saved in your browser. [Read more](dashboard.md#saved-views)

**Export.** Download every run that matches your filters as CSV or JSON.
[Read more](dashboard.md#export)

**Audit log.** Every action taken from the dashboard (cancel, retry, pause,
purge, start, and so on) with who did it and when.
In the dashboard: **Audit log**. [Read more](dashboard.md#audit-log)

## Set it up your way

**Works with your framework.** Mount the dashboard into a FastAPI, Flask or
Django project, or run it on its own. [Read more](integrations.md)

**Any database.** SQLite by default, or PostgreSQL, MySQL and anything else
SQLAlchemy supports, through `db_url`.
[Read more](configuration.md#db_url-backend-notes)

**Upgrades handle themselves.** When a new version changes the database,
it updates the tables when it starts. If you'd rather do that as a deploy
step, run `celery-manager migrate`.
[Read more](configuration.md#schema-and-upgrades)

**Sign-in.** Dashboard users with hashed passwords and session cookies.
There is no default account: you create the first one yourself.
[Read more](quickstart.md#create-your-first-dashboard-user)

**Roles.** Give each account `viewer` (look, don't touch), `operator`
(retry, cancel, pause, worker controls) or `admin` (also purge queues,
shut down workers, change branding). The server enforces it; the dashboard
hides what you can't use.
[Read more](roles.md)

**Themes and branding.** Light, dark or follow the system, chosen per
browser. Set the product name, accent colour and logo once for everyone.
In the dashboard: **Settings**.
[Read more](dashboard.md#appearance-and-branding)

**A REST API for everything.** Every action in the dashboard is also an API
call, so you can script it. [Read more](rest-api.md)
