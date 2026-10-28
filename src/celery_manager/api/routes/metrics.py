from __future__ import annotations

import hmac

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response

from celery_manager.metrics import CONTENT_TYPE

router = APIRouter()


@router.get("/metrics", include_in_schema=False)
def metrics(request: Request):
    """Prometheus text exposition of run counts, durations, failure ratios,
    queue depth, workers online and missed runs.

    Not behind the session cookie, because Prometheus has none. Instead:
    `metrics_token` unset means the endpoint does not exist (404), and when
    set the request must carry `Authorization: Bearer <token>`. GET only, so
    no role applies and the CSRF check never sees it. It is registered before
    the dashboard's static mount, so the single-page app never swallows it.
    """
    token = request.app.state.cm_config.metrics_token
    if not token:
        raise HTTPException(status_code=404, detail="Not Found")

    scheme, _, supplied = (request.headers.get("authorization") or "").partition(" ")
    # compare_digest on bytes: it takes str only if ASCII, and a header can hold anything.
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        supplied.strip().encode("utf-8"), token.encode("utf-8")
    ):
        raise HTTPException(
            status_code=401,
            detail="a valid bearer token is required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return Response(
        content=request.app.state.cm_metrics.render(),
        media_type=CONTENT_TYPE,
        headers={"Cache-Control": "no-store"},
    )
