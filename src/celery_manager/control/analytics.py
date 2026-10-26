"""Query-time analytics over `task_runs`: trends, per-task stats, grouped failures.

Everything here is computed on read from the existing table — no rollup
tables, no schema changes. That keeps it portable (SQLite, Postgres, MySQL
all behave the same, because the only SQL is a filtered column scan) and
means the numbers can never drift from the runs they describe. The price is
that each call scans the rows in its window, so every scan is capped at
`MAX_SCAN_ROWS` (newest first); when the cap bites, the result says
`truncated: true` so the UI can say the figures cover the newest rows only.

Percentiles are computed in Python for the same portability reason: the
databases disagree on percentile functions (or lack them).

An index on `task_runs.started_at` would help the range filter on large
tables; it is deliberately not added here (no schema changes in this change).
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import DateTime, func

from celery_manager.db.models import TaskRun

# Range -> (window, bucket size). Sized so every chart has 12-60 buckets.
RANGES: dict[str, tuple[timedelta, timedelta]] = {
    "1h": (timedelta(hours=1), timedelta(minutes=5)),
    "24h": (timedelta(hours=24), timedelta(hours=1)),
    "7d": (timedelta(days=7), timedelta(hours=6)),
    "30d": (timedelta(days=30), timedelta(days=1)),
}

MAX_SCAN_ROWS = 50_000
MAX_FAILURE_ROWS = 5_000
MAX_GROUP_TASK_IDS = 100
TOP_TASKS = 8
# A task needs a few runs before its failure *rate* means anything: 1 of 1 is
# a 100% failure rate and would top the list on noise alone.
MIN_RUNS_FOR_RATE = 3


class UnknownRange(ValueError):
    pass


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    value = _aware(value)
    return value.isoformat() if value else None


def percentile(sorted_values: list[float], p: float) -> float | None:
    """Linear-interpolated percentile (`p` in 0..100) of an already-sorted list."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = (p / 100) * (len(sorted_values) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return sorted_values[low]
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (rank - low)


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def _success_rate(succeeded: int, failed: int) -> float | None:
    """Succeeded / (succeeded + failed). Runs still in flight, skipped or
    revoked say nothing about reliability, so they are left out of the ratio."""
    done = succeeded + failed
    return None if done == 0 else round(succeeded / done, 4)


def _ts_column():
    return func.coalesce(TaskRun.started_at, TaskRun.finished_at, type_=DateTime(timezone=True))


def _window(range_key: str, now: datetime | None) -> tuple[datetime, datetime, timedelta]:
    if range_key not in RANGES:
        raise UnknownRange(range_key)
    window, bucket = RANGES[range_key]
    until = _aware(now) or datetime.now(timezone.utc)
    return until - window, until, bucket


def _load_rows(session, since: datetime, until: datetime, task_name: str | None, cap: int, failed_only: bool = False):
    ts = _ts_column()
    query = session.query(
        TaskRun.task_id, TaskRun.task_name, TaskRun.status, ts.label("ts"),
        TaskRun.runtime_seconds, TaskRun.exception, TaskRun.traceback,
    ).filter(ts >= since, ts <= until)
    if task_name is not None:
        query = query.filter(TaskRun.task_name == task_name)
    if failed_only:
        query = query.filter(TaskRun.status == "failed")
    rows = query.order_by(TaskRun.id.desc()).limit(cap + 1).all()
    truncated = len(rows) > cap
    return rows[:cap], truncated


def _runtime_stats(runtimes: list[float]) -> dict[str, float | None]:
    runtimes = sorted(runtimes)
    return {
        "p50": _round(percentile(runtimes, 50)),
        "p95": _round(percentile(runtimes, 95)),
        "max": _round(runtimes[-1]) if runtimes else None,
    }


def _task_rollup(rows) -> list[dict[str, Any]]:
    per: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        per[r.task_name]["runs"] += 1
        per[r.task_name][r.status] += 1
    return [
        {
            "task_name": name,
            "runs": c["runs"],
            "failed": c["failed"],
            "failure_rate": round(c["failed"] / c["runs"], 4),
        }
        for name, c in per.items()
    ]


def overview(session, range_key: str = "24h", task_name: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    since, until, bucket = _window(range_key, now)
    rows, truncated = _load_rows(session, since, until, task_name, MAX_SCAN_ROWS)

    size = int(bucket.total_seconds())
    first = int(since.timestamp()) // size * size
    last = int(until.timestamp()) // size * size
    starts = list(range(first, last + 1, size))
    counts: dict[int, Counter] = {s: Counter() for s in starts}
    runtimes: dict[int, list[float]] = {s: [] for s in starts}
    all_runtimes: list[float] = []
    totals: Counter = Counter()

    for r in rows:
        key = int(_aware(r.ts).timestamp()) // size * size
        if key not in counts:  # edge of the window; ignore rather than invent a bucket
            continue
        counts[key][r.status] += 1
        totals[r.status] += 1
        if r.runtime_seconds is not None and r.status in ("succeeded", "failed"):
            runtimes[key].append(r.runtime_seconds)
            all_runtimes.append(r.runtime_seconds)

    series = []
    for s in starts:
        c = counts[s]
        rt = sorted(runtimes[s])
        series.append({
            "start": datetime.fromtimestamp(s, timezone.utc).isoformat(),
            "counts": dict(c),
            "total": sum(c.values()),
            "success_rate": _success_rate(c["succeeded"], c["failed"]),
            "runtime_p50": _round(percentile(rt, 50)),
            "runtime_p95": _round(percentile(rt, 95)),
        })

    total_runs = sum(totals.values())
    stats = _runtime_stats(all_runtimes)
    rollup = _task_rollup(rows) if task_name is None else []
    hours = (until - since).total_seconds() / 3600
    return {
        "range": range_key,
        "task_name": task_name,
        "since": _iso(since),
        "until": _iso(until),
        "bucket_seconds": size,
        "truncated": truncated,
        "totals": {
            "runs": total_runs,
            "by_status": dict(totals),
            "succeeded": totals["succeeded"],
            "failed": totals["failed"],
            "success_rate": _success_rate(totals["succeeded"], totals["failed"]),
            "runtime_p50": stats["p50"],
            "runtime_p95": stats["p95"],
            "throughput_per_hour": round(total_runs / hours, 2),
        },
        "buckets": series,
        "top_by_volume": sorted(rollup, key=lambda t: (-t["runs"], t["task_name"]))[:TOP_TASKS],
        "top_by_failure_rate": sorted(
            (t for t in rollup if t["failed"] > 0 and t["runs"] >= MIN_RUNS_FOR_RATE),
            key=lambda t: (-t["failure_rate"], -t["failed"], t["task_name"]),
        )[:TOP_TASKS],
    }


# --- failure signatures -------------------------------------------------

_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_ADDR = re.compile(r"\b0[xX][0-9a-fA-F]+\b")
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"|`[^`]*`")
# 8+ hex chars containing at least one digit: ids and hashes, not plain words.
_HEX_ID = re.compile(r"\b(?=[0-9a-fA-F]*\d)[0-9a-fA-F]{8,}\b")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_REPR = re.compile(r"^\s*([A-Za-z_][\w.]*)\((.*)\)\s*$", re.DOTALL)
_PLAIN = re.compile(r"^\s*([A-Za-z_][\w.]*):\s*(.*)$", re.DOTALL)
_FRAME = re.compile(r'File "([^"]+)", line \d+, in ([^\s]+)')


def mask_volatile(text: str) -> str:
    """Replace the parts of a message that differ run to run."""
    text = _UUID.sub("<uuid>", text)
    text = _ADDR.sub("<addr>", text)
    text = _QUOTED.sub("<str>", text)
    text = _HEX_ID.sub("<hex>", text)
    text = _NUMBER.sub("<n>", text)
    return re.sub(r"\s+", " ", text).strip()[:240]


def split_exception(exception: str | None) -> tuple[str, str]:
    """(type, raw message) from the stored exception text.

    The decorator stores `repr(exc)` (`ConnectionError('boom')`); other
    writers may store `Type: message`. Anything else becomes type
    "Exception" with the text as the message.
    """
    text = (exception or "").strip()
    if not text:
        return "Unknown", ""
    m = _REPR.match(text)
    if m:
        inner = m.group(2).strip()
        # repr of a one-arg exception is a quoted string; unwrap the outer quotes.
        if len(inner) >= 2 and inner[0] == inner[-1] and inner[0] in "'\"":
            inner = inner[1:-1]
        return m.group(1), inner
    m = _PLAIN.match(text.splitlines()[0])
    if m:
        return m.group(1), m.group(2)
    return "Exception", text


def last_frame(traceback: str | None) -> str | None:
    if not traceback:
        return None
    frames = _FRAME.findall(traceback)
    if not frames:
        return None
    path, func_name = frames[-1]
    return f"{path.replace(chr(92), '/').rsplit('/', 1)[-1]}:{func_name}"


def failure_signature(exception: str | None, traceback: str | None) -> tuple[str, str, str, str | None]:
    """(signature, exception_type, masked_message, frame)."""
    exc_type, message = split_exception(exception)
    masked = mask_volatile(message)
    frame = last_frame(traceback)
    base = f"{exc_type}: {masked}" if masked else exc_type
    return (f"{base} @ {frame}" if frame else base), exc_type, masked, frame


def failures(session, range_key: str = "24h", task_name: str | None = None, now: datetime | None = None, limit: int = 50) -> dict[str, Any]:
    since, until, _ = _window(range_key, now)
    rows, truncated = _load_rows(session, since, until, task_name, MAX_FAILURE_ROWS, failed_only=True)

    groups: dict[str, dict[str, Any]] = {}
    for r in rows:  # newest first, so the first row seen is the freshest example
        signature, exc_type, masked, frame = failure_signature(r.exception, r.traceback)
        ts = _aware(r.ts)
        g = groups.get(signature)
        if g is None:
            g = groups[signature] = {
                "signature": signature,
                "exception_type": exc_type,
                "message": masked,
                "frame": frame,
                "example": (r.exception or "").strip()[:500],
                "count": 0,
                "task_counter": Counter(),
                "first_seen": ts,
                "last_seen": ts,
                "task_ids": [],
            }
        g["count"] += 1
        g["task_counter"][r.task_name] += 1
        if ts is not None:
            if g["first_seen"] is None or ts < g["first_seen"]:
                g["first_seen"] = ts
            if g["last_seen"] is None or ts > g["last_seen"]:
                g["last_seen"] = ts
        if len(g["task_ids"]) < MAX_GROUP_TASK_IDS:
            g["task_ids"].append(r.task_id)

    ordered = sorted(groups.values(), key=lambda g: (-g["count"], g["signature"]))
    items = []
    for g in ordered[: max(1, limit)]:
        items.append({
            "signature": g["signature"],
            "exception_type": g["exception_type"],
            "message": g["message"],
            "frame": g["frame"],
            "example": g["example"],
            "count": g["count"],
            "task_names": [n for n, _ in sorted(g["task_counter"].items(), key=lambda kv: (-kv[1], kv[0]))][:10],
            "first_seen": _iso(g["first_seen"]),
            "last_seen": _iso(g["last_seen"]),
            "example_task_ids": g["task_ids"][:3],
            # Capped: this is what "Retry all in group" sends to retry-bulk.
            "task_ids": g["task_ids"],
            "task_ids_truncated": g["count"] > len(g["task_ids"]),
        })
    return {
        "range": range_key,
        "task_name": task_name,
        "since": _iso(since),
        "until": _iso(until),
        "truncated": truncated,
        "total_failures": len(rows),
        "groups": items,
    }


# --- per-task page ------------------------------------------------------

def task_stats(session, task_name: str, range_key: str = "30d", now: datetime | None = None) -> dict[str, Any] | None:
    """Totals and recent history for one task name, or None if it has no runs."""
    from celery_manager.control.tasks import list_task_runs

    exists = session.query(TaskRun.id).filter(TaskRun.task_name == task_name).first()
    if exists is None:
        return None
    since, until, _ = _window(range_key, now)
    rows, truncated = _load_rows(session, since, until, task_name, MAX_SCAN_ROWS)
    totals: Counter = Counter()
    runtimes: list[float] = []
    for r in rows:
        totals[r.status] += 1
        if r.runtime_seconds is not None and r.status in ("succeeded", "failed"):
            runtimes.append(r.runtime_seconds)

    def last(status: str) -> str | None:
        row = (
            session.query(_ts_column().label("ts"))
            .filter(TaskRun.task_name == task_name, TaskRun.status == status)
            .order_by(TaskRun.id.desc())
            .first()
        )
        return _iso(row.ts) if row else None

    fail = failures(session, range_key, task_name, now=now, limit=5)
    return {
        "task_name": task_name,
        "range": range_key,
        "truncated": truncated,
        "totals": {
            "runs": sum(totals.values()),
            "by_status": dict(totals),
            "succeeded": totals["succeeded"],
            "failed": totals["failed"],
            "success_rate": _success_rate(totals["succeeded"], totals["failed"]),
        },
        "runtime": _runtime_stats(runtimes),
        "last_success_at": last("succeeded"),
        "last_failure_at": last("failed"),
        "recent_runs": list_task_runs(session, task_name=task_name, limit=10),
        "top_errors": fail["groups"],
    }
