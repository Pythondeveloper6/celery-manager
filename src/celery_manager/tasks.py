"""Optional Celery task that runs the retention prune on a schedule.

Opt-in: nothing here is registered unless the host calls
`register_prune_task`, so installing celery-manager never adds a task to a
worker or a beat entry on its own. The prune itself is
`celery_manager.control.retention.run_prune`; the CLI (`celery-manager
prune`) runs the same code for hosts that prefer cron.
"""

from __future__ import annotations

from celery import Celery

from celery_manager.config import CeleryManagerConfig
from celery_manager.control.retention import DEFAULT_BATCH_SIZE, run_prune
from celery_manager.runtime import setup as setup_runtime

PRUNE_TASK_NAME = "celery_manager.tasks.prune"
PRUNE_BEAT_ENTRY = "celery-manager-prune"


def register_prune_task(
    celery_app: Celery,
    config: CeleryManagerConfig,
    *,
    schedule=None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    name: str = PRUNE_TASK_NAME,
):
    """Register the prune task on `celery_app` and return it.

    Call it in the module your workers import (next to `celery_manager.setup`).
    With `schedule` (seconds, a `timedelta` or a `crontab`) it also adds a
    beat entry named `celery-manager-prune`; without it, add the entry to
    your own `beat_schedule` or call `prune.delay()` yourself.

    The task uses the same database as `config` and records itself as the
    user `system` in the audit log. It returns the prune summary.
    """

    @celery_app.task(name=name)
    def prune() -> dict:
        session_factory = setup_runtime(config)
        with session_factory() as session:
            return run_prune(
                session, username="system", batch_size=batch_size,
                sample_retention_days=config.queue_sample_retention_days,
            )

    if schedule is not None:
        entries = dict(celery_app.conf.beat_schedule or {})
        entries[PRUNE_BEAT_ENTRY] = {"task": name, "schedule": schedule}
        celery_app.conf.beat_schedule = entries
    return prune
