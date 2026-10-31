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

from importlib import import_module
from typing import TYPE_CHECKING

# Names resolve on first use (PEP 562) so that importing a light submodule,
# e.g. `celery_manager.observation` in a process without Celery or FastAPI,
# does not drag in the whole package.
_LAZY = {
    "control": ("celery_manager.control", None),
    "CeleryManagerConfig": ("celery_manager.config", "CeleryManagerConfig"),
    "track": ("celery_manager.decorator", "track"),
    "task_url": ("celery_manager.links", "task_url"),
    "RunObserver": ("celery_manager.observation", "RunObserver"),
    "current_run": ("celery_manager.observation", "current_run"),
    "report_progress": ("celery_manager.progress", "report_progress"),
    "install_publish_hook": ("celery_manager.publish", "install_publish_hook"),
    "setup": ("celery_manager.runtime", "setup"),
}

if TYPE_CHECKING:
    from celery_manager import control
    from celery_manager.config import CeleryManagerConfig
    from celery_manager.decorator import track
    from celery_manager.links import task_url
    from celery_manager.observation import RunObserver, current_run
    from celery_manager.progress import report_progress
    from celery_manager.publish import install_publish_hook
    from celery_manager.runtime import setup


def __getattr__(name):
    try:
        module_name, attribute = _LAZY[name]
    except KeyError:
        raise AttributeError(f"module 'celery_manager' has no attribute {name!r}") from None
    module = import_module(module_name)
    value = module if attribute is None else getattr(module, attribute)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_LAZY))


__version__ = "0.3.0a1"

__all__ = [
    "CeleryManagerConfig",
    "RunObserver",
    "__version__",
    "control",
    "current_run",
    "install_publish_hook",
    "report_progress",
    "setup",
    "task_url",
    "track",
]
