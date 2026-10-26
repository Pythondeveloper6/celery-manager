---
title: celery-manager
hide:
  - navigation
  - toc
---

<div class="cm-hero" markdown>

<img class="cm-logo cm-only-light" src="assets/logo-wordmark-light.svg" alt="celery-manager">
<img class="cm-logo cm-only-dark" src="assets/logo-wordmark-dark.svg" alt="celery-manager">

# A dashboard and control API for Celery

<p class="cm-tagline">See what every task is doing, act on it, and pause a single
future occurrence of a periodic task — without touching your beat schedule or
restarting anything. Works with any Django, Flask or FastAPI project that already
uses Celery.</p>

[Get started](quickstart.md){ .md-button .md-button--primary }
[View on GitHub](https://github.com/Pythondeveloper6/celery-manager){ .md-button }

</div>

![The celery-manager task list: filters, status badges, runtimes and start times](assets/screenshots/task-list-light.png){ .cm-shot .cm-only-light }
![The celery-manager task list: filters, status badges, runtimes and start times](assets/screenshots/task-list-dark.png){ .cm-shot .cm-only-dark }

!!! warning "Alpha — `0.1.0a4`"

    Working and tested, but the API may still move, and there is no role system yet
    (any signed-in user can do anything). Use it on an internal tool and pin the
    exact version. The [changelog](changelog.md) records what breaks between
    development builds.

## Sixty seconds

```bash
pip install --pre celery-manager
```

Then mount the dashboard into your existing app and start the events listener next
to your workers. The [Quickstart](quickstart.md) walks through install, the required
Celery settings, creating the first user and signing in.

## What you get

<div class="grid cards" markdown>

-   :material-eye-outline:{ .lg .middle } **Task visibility**

    ---

    Every run, decorated or not: status, args, worker, queue, timings, exceptions
    and full tracebacks.

    [:octicons-arrow-right-24: Dashboard](dashboard.md)

-   :material-history:{ .lg .middle } **History and search**

    ---

    Paginated full run history, filtered by status, task name or time range, with
    free-text search over name, id and arguments.

    [:octicons-arrow-right-24: Filtering and search](rest-api.md#filtering-search-and-pagination)

-   :material-progress-clock:{ .lg .middle } **Live progress**

    ---

    `report_progress()` from inside a task: a count, an optional total and a message,
    throttled so it is safe in a tight loop.

    [:octicons-arrow-right-24: Live progress](progress.md)

-   :material-text-box-outline:{ .lg .middle } **Log capture**

    ---

    The `@track` decorator captures a task's log output and shows it beside the run.

    [:octicons-arrow-right-24: The decorator](decorator.md)

-   :material-stop-circle-outline:{ .lg .middle } **Control**

    ---

    Cancel, terminate and retry tasks (single or bulk) from the UI, and start them through the API, with retry lineage recorded in both
    directions on the detail view.

    [:octicons-arrow-right-24: REST API](rest-api.md)

-   :material-calendar-remove-outline:{ .lg .middle } **Per-occurrence schedule pause**

    ---

    The feature this project exists for: skip or cancel one future run of a periodic
    task, leaving the schedule itself untouched.

    [:octicons-arrow-right-24: Schedules](dashboard.md)

-   :material-clipboard-text-clock-outline:{ .lg .middle } **Audit log**

    ---

    Every control action recorded and attributed to the user who took it.

    [:octicons-arrow-right-24: Dashboard](dashboard.md)

-   :material-palette-outline:{ .lg .middle } **Theming and branding**

    ---

    Light and dark themes, plus a wordmark, accent colour and logo set once per
    deployment and applied on the login screen too.

    [:octicons-arrow-right-24: Configuration](configuration.md)

-   :material-puzzle-outline:{ .lg .middle } **FastAPI, Flask, Django**

    ---

    A FastAPI app you can mount into your own ASGI app, run standalone, or bridge into
    Flask and Django.

    [:octicons-arrow-right-24: Integrations](integrations.md)

</div>

## A closer look

=== "Task list"

    ![The task list with filters, status badges and runtimes](assets/screenshots/task-list-light.png){ .cm-shot .cm-only-light }
    ![The task list with filters, status badges and runtimes](assets/screenshots/task-list-dark.png){ .cm-shot .cm-only-dark }

=== "Task detail"

    ![A running import with a live progress bar, with cancel and terminate controls](assets/screenshots/task-detail-light.png){ .cm-shot .cm-only-light }
    ![A running import with a live progress bar, with cancel and terminate controls](assets/screenshots/task-detail-dark.png){ .cm-shot .cm-only-dark }

=== "Failure & retry"

    ![A failed task showing its traceback, captured logs and a Retry button](assets/screenshots/task-detail-failed-light.png){ .cm-shot .cm-only-light }
    ![A failed task showing its traceback, captured logs and a Retry button](assets/screenshots/task-detail-failed-dark.png){ .cm-shot .cm-only-dark }

=== "Schedules"

    ![The schedules view with one future occurrence skipped](assets/screenshots/schedules-light.png){ .cm-shot .cm-only-light }
    ![The schedules view with one future occurrence skipped](assets/screenshots/schedules-dark.png){ .cm-shot .cm-only-dark }

=== "Audit log"

    ![The audit log of control actions, attributed to users](assets/screenshots/audit-light.png){ .cm-shot .cm-only-light }
    ![The audit log of control actions, attributed to users](assets/screenshots/audit-dark.png){ .cm-shot .cm-only-dark }

## How it works

celery-manager is three cooperating pieces, all pointed at the same database:

1. **The REST API and dashboard** — a FastAPI app serving the JSON API under `/api`
   and the dashboard at `/`. Mount it into your own ASGI app or run it standalone.
2. **The events listener** — `celery-manager events`, a long-running process that
   records every task's status transitions from Celery's event bus, whether or not
   the task uses `@track`.
3. **The `@track` decorator** — optional, per task. It adds log capture, enables
   per-occurrence pause and cancel, and records the run as started before the task
   body runs.

Because they share one SQLAlchemy-backed store, any combination can run in separate
processes or containers. See [Configuration](configuration.md) for `db_url` and the
rest.

## Requirements

Python 3.10+ and an existing Celery project. See the
[Quickstart](quickstart.md#install) for extras.

## Licence

celery-manager is open source under the
[MIT licence](https://github.com/Pythondeveloper6/celery-manager/blob/main/LICENSE),
copyright &copy; 2026 Mahmoud Ahmed.
