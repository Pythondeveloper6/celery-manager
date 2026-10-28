from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from celery_manager.api.deps import get_current_user, require_admin, require_role
from celery_manager.control.retention import (
    get_last_prune,
    get_retention,
    merge_rules,
    prune_audit_log,
    prune_runs,
    update_retention,
)
from celery_manager.control.tasks import list_registered_tasks
from celery_manager.db.models import TaskRun

router = APIRouter(prefix="/api/settings/retention")

# The task-name picker is a convenience; a deployment with an unbounded set of
# generated task names should not turn this response into a megabyte.
_MAX_KNOWN_TASKS = 1000


class RetentionUpdate(BaseModel):
    """Every field optional, so a caller sends only what it changes. The types
    are `Any` on purpose: `control.retention` owns validation and its messages
    are written for the form, where pydantic's list-shaped errors are not."""

    model_config = ConfigDict(extra="forbid")

    default_days: Any = None
    failed_days: Any = None
    audit_days: Any = None
    tasks: Any = None


def _known_tasks(request: Request, session) -> list[str]:
    names = {t["name"] for t in list_registered_tasks(request.app.state.cm_config.celery_app)}
    names.update(r[0] for r in session.query(TaskRun.task_name).distinct().limit(_MAX_KNOWN_TASKS).all())
    return sorted(names)[:_MAX_KNOWN_TASKS]


def _document(request: Request, session) -> dict:
    return {
        **get_retention(session).to_dict(),
        "known_tasks": _known_tasks(request, session),
        "last_prune": get_last_prune(session),
    }


@router.get("")
def read_retention(request: Request, username: str = Depends(get_current_user)):
    """The retention rules, the task names the picker offers, and the last
    prune. Any signed-in account: it holds no secrets and the audit log
    already shows who changed it."""
    with request.app.state.cm_session_factory() as session:
        return _document(request, session)


@router.put("")
def write_retention(payload: RetentionUpdate, request: Request, username: str = Depends(require_admin)):
    """Change the rules. Admin only: this setting decides what gets deleted
    for good, for everyone. `tasks`, when sent, replaces the whole override
    map. Validation failures are a 422 with a sentence for the form."""
    values = payload.model_dump(exclude_unset=True)
    with request.app.state.cm_session_factory() as session:
        try:
            update_retention(session, values, username)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        return _document(request, session)


@router.post("/preview")
def preview_retention(
    request: Request,
    payload: RetentionUpdate | None = None,
    username: str = Depends(require_role("viewer")),
):
    """What a prune would delete right now, without deleting anything.

    With a body, previews that candidate (the stored rules with the sent keys
    replaced) so the form can show the effect before it is saved; without one,
    previews the stored rules. A POST only because the candidate is a body.
    Read-only, hence the viewer role: it reveals counts, nothing the lists
    don't already show.
    """
    values = payload.model_dump(exclude_unset=True) if payload else {}
    with request.app.state.cm_session_factory() as session:
        try:
            rules = merge_rules(get_retention(session), values)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        by_task = prune_runs(session, dry_run=True, rules=rules)
        audit = prune_audit_log(session, dry_run=True, rules=rules)
        return {
            "runs": sum(by_task.values()),
            "audit_log": audit,
            "by_task": by_task,
        }
