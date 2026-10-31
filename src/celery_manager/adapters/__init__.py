"""Record runs from task runners other than Celery.

Each adapter turns its runner's lifecycle into `RunObserver` observations:
queued (where the runner exposes enqueueing), started, then succeeded,
failed, skipped or interrupted. They observe only: the runner keeps
scheduling, retrying and executing exactly as before, and a storage outage
never fails a job while the observer is fail-open (the default).

- `celery_manager.adapters.loop` - plain asyncio or thread loops
  (`run_every`, `observed_job`).
- `celery_manager.adapters.dramatiq` - `CeleryManagerMiddleware`.
- `celery_manager.adapters.taskiq` - `CeleryManagerMiddleware`.
- `celery_manager.adapters.arq` - `observed` job wrapper.

Each module imports its runner lazily or only when imported itself, so
installing celery-manager never pulls these runners in.
"""
