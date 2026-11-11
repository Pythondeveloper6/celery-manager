"""LLM usage queries over `llm_calls`: totals, per model, per task, over time."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func

from celery_manager.control.analytics import RANGES, UnknownRange
from celery_manager.db.llm import LlmCall

TOP_ROWS = 20
MAX_SCAN_ROWS = 50_000


def _since(time_range: str, now: datetime | None):
    if time_range not in RANGES:
        raise UnknownRange(time_range)
    window, bucket = RANGES[time_range]
    now = now or datetime.now(timezone.utc)
    return now - window, now, bucket


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _row(calls, input_tokens, output_tokens, cache_read, tool_calls, cost, avg_ms) -> dict:
    return {
        "calls": calls, "input_tokens": int(input_tokens), "output_tokens": int(output_tokens),
        "cache_read_tokens": int(cache_read), "tool_calls": int(tool_calls),
        "cost_usd": None if cost is None else round(cost, 6),
        "avg_duration_ms": None if avg_ms is None else round(avg_ms, 1),
    }


_COLUMNS = (
    func.count(), func.coalesce(func.sum(LlmCall.input_tokens), 0), func.coalesce(func.sum(LlmCall.output_tokens), 0),
    func.coalesce(func.sum(LlmCall.cache_read_tokens), 0), func.coalesce(func.sum(LlmCall.tool_calls), 0),
    func.sum(LlmCall.cost_usd), func.avg(LlmCall.duration_ms),
)


def usage_summary(session, time_range: str = "24h", task_name: str | None = None, *, now: datetime | None = None) -> dict:
    """Totals, per-model and per-task breakdown and a time series for the range."""
    since, until, bucket = _since(time_range, now)

    def scoped(query):
        query = query.filter(LlmCall.created_at >= since, LlmCall.created_at <= until)
        return query.filter(LlmCall.task_name == task_name) if task_name else query

    totals = scoped(session.query(*_COLUMNS)).one()
    errors = scoped(session.query(func.count()).filter(LlmCall.status == "error")).scalar() or 0
    by_model = [
        {"model": model, **_row(*vals)}
        for model, *vals in scoped(session.query(LlmCall.model, *_COLUMNS)).group_by(LlmCall.model)
        .order_by(func.count().desc()).limit(TOP_ROWS)
    ]
    by_task = [
        {"task_name": name, **_row(*vals)}
        for name, *vals in scoped(session.query(LlmCall.task_name, *_COLUMNS).filter(LlmCall.task_name.isnot(None)))
        .group_by(LlmCall.task_name).order_by(func.coalesce(func.sum(LlmCall.cost_usd), 0).desc(), func.count().desc())
        .limit(TOP_ROWS)
    ]
    rows = scoped(session.query(LlmCall.created_at, LlmCall.input_tokens, LlmCall.output_tokens, LlmCall.cost_usd)
                  ).order_by(LlmCall.created_at.desc()).limit(MAX_SCAN_ROWS).all()
    count = int(since.timestamp() // bucket.total_seconds())
    buckets: dict[int, dict] = {}
    for created, tin, tout, cost in rows:
        index = int(_aware(created).timestamp() // bucket.total_seconds())
        slot = buckets.setdefault(index, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
        slot["calls"] += 1
        slot["input_tokens"] += tin or 0
        slot["output_tokens"] += tout or 0
        slot["cost_usd"] += cost or 0.0
    last = int(until.timestamp() // bucket.total_seconds())
    series = []
    for index in range(count + 1, last + 1):
        slot = buckets.get(index, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
        series.append({"at": datetime.fromtimestamp(index * bucket.total_seconds(), timezone.utc).isoformat(),
                       **slot, "cost_usd": round(slot["cost_usd"], 6)})
    priced = scoped(session.query(func.count()).filter(LlmCall.cost_usd.isnot(None))).scalar() or 0
    return {
        "range": time_range, "since": since.isoformat(), "until": until.isoformat(),
        "totals": {**_row(*totals), "errors": errors, "unpriced_calls": totals[0] - priced},
        "by_model": by_model, "by_task": by_task, "series": series,
        "truncated": len(rows) >= MAX_SCAN_ROWS,
    }


def run_usage(session, task_id: str) -> dict:
    """Every recorded call of one run, plus their totals."""
    calls = session.query(LlmCall).filter(LlmCall.task_id == task_id).order_by(LlmCall.created_at, LlmCall.id).all()
    items = [{
        "model": c.model, "provider": c.provider, "input_tokens": c.input_tokens, "output_tokens": c.output_tokens,
        "cache_read_tokens": c.cache_read_tokens, "cache_write_tokens": c.cache_write_tokens,
        "tool_calls": c.tool_calls, "cost_usd": c.cost_usd, "duration_ms": c.duration_ms, "status": c.status,
        "created_at": _aware(c.created_at).isoformat(),
    } for c in calls]
    priced = [c.cost_usd for c in calls if c.cost_usd is not None]
    return {
        "task_id": task_id, "calls": items,
        "totals": {"calls": len(items), "input_tokens": sum(c.input_tokens for c in calls),
                   "output_tokens": sum(c.output_tokens for c in calls),
                   "cost_usd": round(sum(priced), 6) if priced else None},
    }


def prune_llm_calls(session, retention_days: int, *, now: datetime | None = None, dry_run: bool = False) -> int:
    if not retention_days:
        return 0
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
    count = session.query(LlmCall).filter(LlmCall.created_at < cutoff).count()
    if not dry_run and count:
        session.execute(delete(LlmCall).where(LlmCall.created_at < cutoff))
        session.commit()
    return count
