"""One dispatch policy for start, retry, failure reruns and schedule run-now."""
from __future__ import annotations

import inspect
from dataclasses import dataclass

from celery_manager.control.audit import record


@dataclass
class DispatchDenied(ValueError):
    message: str
    status_code: int = 403

    def __str__(self):
        return self.message


def task_allowed(config, name, principal):
    allowed = config.startable_tasks
    return allowed is None or (allowed(name, principal) if callable(allowed) else name in allowed)


def validate_dispatch(config, name, args, kwargs, *, queue=None, edited=False, session=None, principal=None):
    if config is None:
        return
    try:
        app = config.celery_app
        if app is None:
            raise DispatchDenied("Observation-only deployments cannot dispatch Celery tasks.", 409)
        app.loader.import_default_modules()
        if name.startswith("celery.") or name not in app.tasks:
            raise DispatchDenied("Task is not registered on this Celery app.", 422)
        if not task_allowed(config, name, principal):
            raise DispatchDenied("This task is not allowed to be started from celery-manager.")
        if queue is not None and not config.allow_queue_override:
            raise DispatchDenied("Queue overrides are disabled.")
        if edited and not config.retry_edit_args:
            raise DispatchDenied("Editing retry arguments is disabled.")
        schema = config.args_schemas.get(name)
        if schema is not None:
            try:
                bound = inspect.signature(app.tasks[name].run).bind(*args, **kwargs)
                bound.apply_defaults()
                schema.model_validate(bound.arguments, strict=True)
            except (ValueError, TypeError) as exc:
                raise DispatchDenied(f"Task arguments failed validation: {exc}", 422) from exc
    except DispatchDenied as exc:
        if session is not None:
            record(session, getattr(principal, "username", None) or "unknown", "start_denied", "task", name,
                   {"reason": str(exc)}, actor_subject=getattr(principal, "subject", None))
        raise
