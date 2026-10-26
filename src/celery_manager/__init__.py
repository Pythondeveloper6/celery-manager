"""celery-manager — a dashboard and control API for Celery background tasks.

The public surface spec §4 assumes, importable straight from the package
root::

    import celery_manager
    from celery_manager import CeleryManagerConfig, setup, track, report_progress

    celery_manager.setup(config)        # wire this process (worker or API)

    @app.task(bind=True)
    @celery_manager.track()
    def send_report(self): ...

`report_progress(current, total=None, message=None)` is called from *inside*
a task body to publish live progress to the dashboard while the task is still
running. It is throttled, tolerant of bad arguments, and — like everything
else here — never raises into the task that called it::

    @app.task(bind=True)
    @celery_manager.track()
    def import_rows(self, rows):
        for i, row in enumerate(rows):
            handle(row)
            celery_manager.report_progress(i + 1, len(rows), f"row {i + 1}")

`celery_manager.control` (task/schedule/audit operations) is re-exported here
too, so `celery_manager.control.list_task_runs(...)` works without a separate
import of the submodule.
"""

from celery_manager import control
from celery_manager.config import CeleryManagerConfig
from celery_manager.decorator import track
from celery_manager.progress import report_progress
from celery_manager.runtime import setup

__version__ = "0.1.0a4"

__all__ = [
    "CeleryManagerConfig",
    "__version__",
    "control",
    "report_progress",
    "setup",
    "track",
]
