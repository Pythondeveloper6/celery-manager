"""Queue wait time: how long runs sat in the queue before a worker started them.

    wait = started_at - max(sent_at, eta)

`sent_at` is the task-sent event's timestamp. A task with an ETA or countdown
is *meant* to sit until its ETA, so the wait is measured from the later of the
two: a task sent at 10:00 with `countdown=300` that starts at 10:05:02 waited
2 seconds, not 302. Runs with no `sent_at` (the task-sent event was never seen:
sent by a client that doesn't emit it, or before the listener ran) are left
out rather than guessed at. A negative result (the producer's clock runs ahead
of the worker's) is clamped to 0, because a wait can't be below zero and the
run did start promptly.

Percentiles without a SQL percentile function
---------------------------------------------
`percentile_cont` exists on PostgreSQL only, and this package runs on SQLite
and MySQL as well. So the window's raw (queue, task, sent, eta, started) values
are fetched and the percentiles computed here, in Python, with linear
interpolation. The trade-off is memory and transfer: `ROW_CAP` bounds it by
reading the newest `ROW_CAP` runs of the window only. Under that cap the
numbers are exact; above it they describe the most recent runs and the result
says `truncated: true`. The window is short (minutes), so the cap is reached
only by deployments running tens of thousands of tasks in it, where the newest
50 000 are a representative sample anyway.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from celery_manager.db.models import TaskRun

# Newest runs read per query; see the module docstring.
ROW_CAP = 50_000
DEFAULT_WINDOW = timedelta(minutes=15)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def wait_seconds(sent_at: datetime | None, eta: datetime | None, started_at: datetime | None) -> float | None:
    """The wait for one run, or None when it can't be computed."""
    if sent_at is None or started_at is None:
        return None
    ready = _aware(sent_at)
    if eta is not None:
        ready = max(ready, _aware(eta))
    return max(0.0, (_aware(started_at) - ready).total_seconds())


def percentile(sorted_values: list[float], q: float) -> float:
    """The `q`-th percentile (0-100) of an ascending list, by linear
    interpolation between closest ranks (the common "type 7" definition)."""
    if not sorted_values:
        raise ValueError("percentile of no values")
    q = min(100.0, max(0.0, q))
    rank = (len(sorted_values) - 1) * q / 100.0
    low = int(rank)
    high = min(low + 1, len(sorted_values) - 1)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (rank - low)


def summarise(values: list[float]) -> dict:
    ordered = sorted(values)
    return {
        "runs": len(ordered),
        "p50": percentile(ordered, 50),
        "p95": percentile(ordered, 95),
        "max": ordered[-1],
    }


class WaitSamples:
    """The raw waits of one window, grouped, for callers that need more than
    p50/p95/max (the alert rule's configurable quantile)."""

    def __init__(self, window: timedelta, truncated: bool):
        self.window = window
        self.truncated = truncated
        self.overall: list[float] = []
        self.by_queue: dict[str, list[float]] = {}
        self.by_task: dict[str, list[float]] = {}

    def stats(self) -> dict:
        """`{"window_seconds", "truncated", "overall", "by_queue", "by_task"}`.

        Each entry is `{"runs", "p50", "p95", "max"}` in seconds; `overall` is
        None when no run qualifies. Runs without a queue count in `overall`
        and `by_task` only.
        """
        return {
            "window_seconds": int(self.window.total_seconds()),
            "truncated": self.truncated,
            "overall": summarise(self.overall) if self.overall else None,
            "by_queue": {k: summarise(v) for k, v in sorted(self.by_queue.items())},
            "by_task": {k: summarise(v) for k, v in sorted(self.by_task.items())},
        }


def wait_samples(
    session,
    *,
    now: datetime | None = None,
    window: timedelta = DEFAULT_WINDOW,
    queue: str | None = None,
    task_name: str | None = None,
    row_cap: int = ROW_CAP,
) -> WaitSamples:
    """Waits of the runs that *started* inside the window (newest `row_cap`
    of them). `queue` / `task_name` narrow the runs considered."""
    now = _aware(now) if now else datetime.now(timezone.utc)
    since = now - window
    query = session.query(
        TaskRun.queue, TaskRun.task_name, TaskRun.sent_at, TaskRun.eta, TaskRun.started_at
    ).filter(
        TaskRun.sent_at.isnot(None),
        TaskRun.started_at >= since,
        TaskRun.started_at <= now,
    )
    if queue is not None:
        query = query.filter(TaskRun.queue == queue)
    if task_name is not None:
        query = query.filter(TaskRun.task_name == task_name)
    rows = query.order_by(TaskRun.started_at.desc()).limit(row_cap + 1).all()
    samples = WaitSamples(window, truncated=len(rows) > row_cap)
    for q, name, sent_at, eta, started_at in rows[:row_cap]:
        wait = wait_seconds(sent_at, eta, started_at)
        if wait is None:
            continue
        samples.overall.append(wait)
        samples.by_task.setdefault(name, []).append(wait)
        if q:
            samples.by_queue.setdefault(q, []).append(wait)
    return samples


def wait_stats(session, **kwargs) -> dict:
    """`wait_samples(...).stats()`; see there for the shape."""
    return wait_samples(session, **kwargs).stats()
