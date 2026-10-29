from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from celery_manager.api.deps import get_current_user
from celery_manager.api.serialization import utc_bound
from celery_manager.control.audit import record as record_audit
from celery_manager.control.export import count_export_rows, iter_export

router = APIRouter(prefix="/api/tasks")


@router.get("/export")
def export_tasks(
    request: Request,
    format: Literal["csv", "json"] = "csv",
    status: str | None = None,
    task_name: str | None = None,
    q: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    initiator: str | None = None,
    started_by_me: bool = False,
    username: str = Depends(get_current_user),
):
    """Download every task run matching the list filters, as CSV or JSON.

    Takes the same filter parameters as `GET /api/tasks`. Streamed, capped at
    `CeleryManagerConfig.export_max_rows`; `X-Export-Truncated: true` says the
    cap cut the result short. Must be included in the app *before* the tasks
    router so `/export` is not captured by `/{task_id}`.
    """
    config = request.app.state.cm_config
    max_rows = config.export_max_rows
    filters = {
        "status": status, "task_name": task_name, "q": q,
        "date_from": utc_bound(date_from), "date_to": utc_bound(date_to),
            "initiator": (getattr(username, "subject", None) or str(username)) if started_by_me else initiator,
    }
    session_factory = request.app.state.cm_session_factory

    with session_factory() as session:
        total, rows = count_export_rows(session, max_rows, **filters)
        record_audit(
            session, username, "export_tasks", "task", "*",
            {
                "format": format, "rows": rows, "total": total, "truncated": total > rows,
                "filters": {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in filters.items() if v is not None},
            },
        )

    def body():
        # The request-scoped session above is closed by now; the stream opens
        # its own so it lives exactly as long as the download.
        with session_factory() as stream_session:
            yield from iter_export(stream_session, format, rows, **filters)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    headers = {
        "Content-Disposition": f'attachment; filename="task-runs-{stamp}.{format}"',
        "X-Export-Rows": str(rows),
        "X-Export-Total": str(total),
        "X-Export-Truncated": "true" if total > rows else "false",
        "Cache-Control": "no-store",
    }
    media = "text/csv; charset=utf-8" if format == "csv" else "application/json"
    return StreamingResponse(body(), media_type=media, headers=headers)
