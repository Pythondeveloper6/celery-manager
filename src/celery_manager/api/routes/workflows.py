from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from celery_manager.api.deps import get_current_user
from celery_manager.api.serialization import utc
from celery_manager.control.workflows import get_workflow

router = APIRouter(prefix="/api/tasks")


@router.get("/{task_id}/workflow")
def workflow(task_id: str, request: Request, username: str = Depends(get_current_user)):
    """The chain/group/chord tree the run belongs to (a lone run is a tree of one)."""
    with request.app.state.cm_session_factory() as session:
        result = get_workflow(session, task_id)
        if result is None:
            raise HTTPException(status_code=404, detail="not found")
        nodes = [
            {
                "task_id": r.task_id,
                "task_name": r.task_name,
                "status": r.status,
                "started_at": utc(r.started_at),
                "finished_at": utc(r.finished_at),
                "runtime_seconds": r.runtime_seconds,
                "parent_id": r.parent_id,
                "group_id": r.group_id,
            }
            for r in result["nodes"]
        ]
        ids = {n["task_id"] for n in nodes}
        return {
            "root_id": result["root_id"],
            "current": result["current"],
            "nodes": nodes,
            "edges": [{"from": n["parent_id"], "to": n["task_id"]} for n in nodes if n["parent_id"] in ids],
            "truncated": result["truncated"],
        }
