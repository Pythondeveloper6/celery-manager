from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator
from datetime import datetime, timezone

from celery_manager.control.tasks import _apply_task_filters
from celery_manager.db.models import TaskRun

# Default hard ceiling on rows in one export; overridable through
# `CeleryManagerConfig.export_max_rows`.
DEFAULT_EXPORT_MAX_ROWS = 50_000

EXPORT_COLUMNS = [
    "task_id", "task_name", "status", "queue", "worker", "args", "kwargs",
    "started_at", "finished_at", "runtime_seconds", "retry_count", "exception", "schedule_name",
]

# How many rows are pulled from the database per round trip.
_BATCH = 500

# Characters a spreadsheet reads as the start of a formula. Tab and CR are
# included because Excel also treats those as leading formula triggers.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value) -> str:
    """Render a cell as text, defusing spreadsheet formula injection.

    Task names, args and exception text are attacker-influenced (they come
    from whatever called the task), and a cell like `=HYPERLINK(...)` would
    execute when an operator opens the export in Excel. Prefixing a single
    quote makes the spreadsheet treat it as text.
    """
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    if text.startswith(_FORMULA_PREFIXES):
        return "'" + text
    return text


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _row(run: TaskRun) -> dict:
    return {
        "task_id": run.task_id,
        "task_name": run.task_name,
        "status": run.status,
        "queue": run.queue,
        "worker": run.worker,
        "args": run.args,
        "kwargs": run.kwargs,
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "runtime_seconds": run.runtime_seconds,
        "retry_count": run.retry_count,
        "exception": run.exception,
        "schedule_name": run.schedule_name,
    }


def _decode_json(raw):
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def count_export_rows(session, max_rows: int, **filters) -> tuple[int, int]:
    """(total matching, rows that will actually be exported)."""
    total = _apply_task_filters(session.query(TaskRun.id), **filters).count()
    return total, min(total, max_rows)


def iter_export(session, fmt: str, limit: int, **filters) -> Iterator[str]:
    """Yield the export body in chunks, newest first, at most `limit` rows.

    Rows are streamed from the database in batches (`yield_per`), so memory
    stays flat regardless of table size. Filtering goes through the same
    `_apply_task_filters` the list endpoint uses.
    """
    query = _apply_task_filters(session.query(TaskRun), **filters).order_by(TaskRun.id.desc()).limit(limit)
    runs = query.yield_per(_BATCH)

    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(EXPORT_COLUMNS)
        yield buf.getvalue()
        for run in runs:
            buf.seek(0)
            buf.truncate()
            data = _row(run)
            writer.writerow([csv_safe(data[c]) for c in EXPORT_COLUMNS])
            yield buf.getvalue()
    else:
        yield "["
        first = True
        for run in runs:
            data = _row(run)
            data["args"] = _decode_json(data["args"])
            data["kwargs"] = _decode_json(data["kwargs"])
            yield ("" if first else ",") + "\n" + json.dumps(data)
            first = False
        yield "\n]\n"
