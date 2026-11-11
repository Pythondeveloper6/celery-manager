from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request

from celery_manager.api.deps import get_current_user
from celery_manager.control import llm as llm_control

router = APIRouter()

RangeKey = Literal["1h", "24h", "7d", "30d"]


@router.get("/api/llm/usage")
def get_llm_usage(
    request: Request,
    time_range: Annotated[RangeKey, Query(alias="range")] = "24h",
    task_name: str | None = None,
    username: str = Depends(get_current_user),
):
    """LLM calls recorded by tasks: totals, per model, per task and over time."""
    with request.app.state.cm_session_factory() as session:
        return llm_control.usage_summary(session, time_range, task_name)


@router.get("/api/llm/runs/{task_id}")
def get_run_llm_usage(task_id: str, request: Request, username: str = Depends(get_current_user)):
    """Every LLM call one run recorded, with their totals."""
    with request.app.state.cm_session_factory() as session:
        return llm_control.run_usage(session, task_id)
