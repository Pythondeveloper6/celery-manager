# Other task runners

celery-manager's history, failure inbox, alerts and dashboard also work for
jobs that never touch Celery. The adapters below turn a runner's lifecycle
into [`RunObserver`](real-world-operations.md#observe-host-owned-work)
observations. They **observe only**: the runner keeps scheduling, retrying and
executing exactly as before. Celery-only controls (pause one occurrence,
retry from the dashboard, revoke, rate limits) do not apply to these runs.

All adapters share one observer, built with `celery_app=None`:

```python
from celery_manager import CeleryManagerConfig, RunObserver

config = CeleryManagerConfig(
    celery_app=None,
    db_url="postgresql+psycopg://monitor:...@db/app",
    db_schema="job_monitoring",
)
observer = RunObserver(config)
```

A process that only observes imports neither Celery nor FastAPI nor bcrypt;
those load when you mount the dashboard or use `@track`.

| Runner | Module | What is recorded | Run ID |
|---|---|---|---|
| asyncio loop / thread loop | `celery_manager.adapters.loop` | started, succeeded / failed, interrupted on cancel | generated per run |
| Dramatiq | `celery_manager.adapters.dramatiq` | queued (producer), started, succeeded / failed, skipped | `<message_id>:<retry>` |
| Taskiq | `celery_manager.adapters.taskiq` | queued (producer), started, succeeded / failed | `task_id` |
| arq | `celery_manager.adapters.arq` | started, succeeded / failed, interrupted on cancel or timeout | `<job_id>:<job_try>` |

Run IDs are stored with an `external:` prefix, so they never collide with
Celery task IDs.

## Plain asyncio or thread loops

For an in-process scheduler loop (`while True: ...; await asyncio.sleep(n)`),
either wrap the job or let celery-manager drive the interval:

```python
import asyncio
from celery_manager.adapters.loop import observed_job, run_every, run_every_in_thread

@observed_job(observer, "reports.nightly")
async def nightly_report():
    ...

# Every 5 minutes, start to start. A failing run is recorded and logged;
# the loop continues. Cancelling the task records the in-flight run as
# interrupted.
refresh_task = asyncio.create_task(run_every(observer, "cache.refresh", refresh_cache, seconds=300))

# Sync jobs in a daemon thread; set thread.stop_event to stop it.
thread = run_every_in_thread(observer, "files.sweep", sweep, seconds=60)
```

A sync job given to `run_every` runs in a worker thread, so it never blocks
the event loop. Missed slots after an overrun are not replayed.

## Dramatiq

```python
from celery_manager.adapters.dramatiq import CeleryManagerMiddleware

broker.add_middleware(CeleryManagerMiddleware(observer))
```

Add the middleware in both producers and workers. Each retry is its own run,
so a failed first attempt and a successful retry both appear. Pass
`record_enqueue=False` to skip the producer-side `queued` write.

## Taskiq

```python
from celery_manager.adapters.taskiq import CeleryManagerMiddleware

broker = broker.with_middlewares(CeleryManagerMiddleware(observer))
```

Database writes run in a thread, never on the event loop. Pass
`record_send=False` to skip the producer-side `queued` write.

## arq

arq's job hooks do not see the job's outcome, so wrap the functions:

```python
from celery_manager.adapters.arq import observed

observe = observed(observer)

@observe
async def send_invoice(ctx, invoice_id):
    ...

class WorkerSettings:
    functions = [send_invoice]
```

The run name defaults to `module.function`; pass `observed(observer,
name="billing.send_invoice")` to choose one. Every exception is re-raised,
so `Retry`, timeouts and `max_tries` behave as before.

## Failure behaviour

The observer is fail-open by default (`observer_fail_open=True`): if the
database is down, the job still runs and `observer.health()` counts the failed
writes. Set it to `False` only when a missing record should stop the job.
