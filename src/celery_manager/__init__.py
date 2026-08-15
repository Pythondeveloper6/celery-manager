"""celery-manager — a dashboard and control API for Celery background tasks.

The public surface spec §4 assumes, importable straight from the package
root::

    import celery_manager
    from celery_manager import CeleryManagerConfig, setup, track

    celery_manager.setup(config)        # wire this process (worker or API)

    @app.task(bind=True)
    @celery_manager.track()
    def send_report(self): ...

`celery_manager.control` (task/schedule/audit operations) is re-exported here
too, so `celery_manager.control.list_task_runs(...)` works without a separate
import of the submodule.
"""

from celery_manager import control
from celery_manager.config import CeleryManagerConfig
from celery_manager.decorator import track
from celery_manager.runtime import setup

__version__ = "0.1.0"

__all__ = [
    "CeleryManagerConfig",
    "__version__",
    "control",
    "setup",
    "track",
]
