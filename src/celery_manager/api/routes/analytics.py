from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from celery_manager.api.deps import get_current_user
from celery_manager.api.serialization import utc
from celery_manager.control import analytics

router = APIRouter(prefix="/api/analytics")

RangeKey = Literal["1h", "24h", "7d", "30d"]


@router.get("/overview")
def get_overview(
    request: Request,
    time_range: Annotated[RangeKey, Query(alias="range")] = "24h",
    task_name: str | None = None,
    username: str = Depends(get_current_user),
):
    """Time-bucketed runs by status, success rate and runtime percentiles."""
    with request.app.state.cm_session_factory() as session:
        return analytics.overview(session, time_range, task_name)


@router.get("/failures")
def get_failures(
    request: Request,
    time_range: Annotated[RangeKey, Query(alias="range")] = "24h",
    task_name: str | None = None,
    username: str = Depends(get_current_user),
):
    """Failed runs grouped by a normalised exception signature."""
    with request.app.state.cm_session_factory() as session:
        return analytics.failures(session, time_range, task_name)


@router.get("/tasks/{task_name}")
def get_task_stats(
    task_name: str,
    request: Request,
    time_range: Annotated[RangeKey, Query(alias="range")] = "30d",
    username: str = Depends(get_current_user),
):
    """Stats for one task name; 404 if it has never run."""
    with request.app.state.cm_session_factory() as session:
        stats = analytics.task_stats(session, task_name, time_range)
        if stats is None:
            raise HTTPException(status_code=404, detail="not found")
        stats["recent_runs"] = [
            {
                "task_id": r.task_id,
                "task_name": r.task_name,
                "status": r.status,
                "started_at": utc(r.started_at),
                "finished_at": utc(r.finished_at),
                "runtime_seconds": r.runtime_seconds,
                "progress_current": r.progress_current,
                "progress_total": r.progress_total,
                "progress_message": r.progress_message,
                "progress_updated_at": utc(r.progress_updated_at),
            }
            for r in stats["recent_runs"]
        ]
        return stats
