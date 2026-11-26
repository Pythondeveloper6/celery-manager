"""A Celery task base class that tracks every task without a decorator.

    app = Celery("shop", task_cls="celery_manager.task:TrackedTask")

Each task then runs through the same wrapper as `@track()` (logs, progress,
pause, maintenance windows, occurrence stamps, redaction). Limit it with
`CeleryManagerConfig.track_tasks` / `track_exclude` (fnmatch globs on the task
name). A task that also carries `@track(...)` keeps the decorator's options
and its body is wrapped once.
"""
from __future__ import annotations

from fnmatch import fnmatchcase

from celery import Task

from celery_manager.decorator import _state_for_app, track


def _run_body(task, *args, **kwargs):
    return task.run(*args, **kwargs)


_tracked_body = track()(_run_body)


def _globs(value) -> tuple[str, ...]:
    return (value,) if isinstance(value, str) else tuple(value or ())


def is_covered(task_name: str, config) -> bool:
    """Whether `TrackedTask` tracks `task_name` under `config` (None: everything)."""
    include = _globs(getattr(config, "track_tasks", "*"))
    exclude = _globs(getattr(config, "track_exclude", ()))
    return any(fnmatchcase(task_name, g) for g in include) and not any(fnmatchcase(task_name, g) for g in exclude)


class TrackedTask(Task):
    def __call__(self, *args, **kwargs):
        if (
            self.request.called_directly
            or getattr(self.run, "_cm_tracked", False)
            or not is_covered(self.name, _state_for_app(self.app).config)
        ):
            # The worker has already pushed the request; Task.__call__ would
            # replace it with one that has no id, so run the body directly.
            return super().__call__(*args, **kwargs) if self.request.called_directly else self.run(*args, **kwargs)
        return _tracked_body(self, *args, **kwargs)
