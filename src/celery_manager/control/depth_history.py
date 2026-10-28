"""Queue depth history: sample broker depth over time, chart it, alert on growth.

Sampling
--------
`sample_once` reads every known queue's depth with `control.queues.list_queues`
(so it inherits that function's connect timeout and hard deadline: a dead
broker costs one deadline per tick, never a hang) and stores one row per queue
in `queue_depth_samples`. Only depths the broker reported are stored. A queue
whose depth is unknown (broker unreachable, transport that can't say, a
RabbitMQ queue nobody has declared) is skipped: a stored 0 would read as
"empty" on the chart and would hide the outage in the `queue_growing` rule.

`start_in_background` runs it on a daemon thread of the events listener
process (`celery-manager events`), every `queue_sample_interval_seconds`.

Several listener processes: the sampler *dedupes per interval* on a
best-effort basis. Before sampling, a process looks at the newest sample row
(any queue); if it is younger than 60% of the interval another process has
just sampled and this tick is skipped. The check is a read, not a lock, so two
processes ticking in the same instant can still both write; that costs one
extra point per queue, which the chart's bucketing and the growth rule both
tolerate. Neither correctness nor storage depends on exactly-once.

Retention
---------
Rows older than `queue_sample_retention_days` are deleted by
`prune_samples`, called from `control.retention.run_prune` and, at most once
an hour, by the sampler itself, so a deployment that never schedules the
retention task still doesn't grow without bound.

Growth
------
`growth` decides whether a series is "rising monotonically-ish"; its
docstring has the exact rule.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func

from celery_manager.db.models import QueueDepthSample

logger = logging.getLogger(__name__)

DEFAULT_SAMPLE_INTERVAL = 60.0
DEFAULT_RETENTION_DAYS = 14
# A tick is skipped when the newest sample is younger than this share of the
# interval (see the module docstring).
DEDUPE_SHARE = 0.6
# How often the sampler prunes on its own.
SELF_PRUNE_EVERY = timedelta(hours=1)

MAX_HISTORY_HOURS = 24 * 30
# Points returned by the history endpoint. A sparkline and a chart gain
# nothing from more, and it bounds the response size.
MAX_POINTS = 240
# Rows read to build one history response: 30 days at one sample a minute.
ROW_CAP = 50_000

# `growth` thresholds.
MIN_GROWTH_SAMPLES = 3
MAX_DECREASING_STEP_SHARE = 0.2
MAX_DIP_SHARE_OF_RISE = 0.5


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _aware(value).isoformat()


# --- sampling -----------------------------------------------------------------


def _depths_from_listing(listing: dict) -> dict[str, int]:
    depths: dict[str, int] = {}
    for queue in listing.get("queues") or []:
        depth = queue.get("depth")
        if depth is None:
            continue
        # exists == False with a reason is a RabbitMQ queue nobody declared:
        # unknown, not empty. On Redis a missing key has no reason and is a
        # true 0.
        if queue.get("exists") is False and queue.get("reason"):
            continue
        depths[queue["name"][:255]] = int(depth)
    return depths


def sample_once(
    session_factory,
    celery_app,
    *,
    interval: float = DEFAULT_SAMPLE_INTERVAL,
    now: datetime | None = None,
    list_queues_fn=None,
) -> int:
    """Take one sample of every queue; returns how many rows were written
    (0 when skipped as a duplicate or when no depth is known)."""
    if list_queues_fn is None:
        from celery_manager.control.queues import list_queues as list_queues_fn
    now = _aware(now) if now else datetime.now(timezone.utc)
    with session_factory() as session:
        newest = session.query(func.max(QueueDepthSample.sampled_at)).scalar()
        if newest is not None and now - _aware(newest) < timedelta(seconds=interval * DEDUPE_SHARE):
            return 0
        depths = _depths_from_listing(list_queues_fn(celery_app, session, include_workers=False))
        for name, depth in depths.items():
            session.add(QueueDepthSample(sampled_at=now, queue=name, depth=depth))
        session.commit()
        return len(depths)


def run_forever(session_factory, celery_app, interval: float, stop_event: threading.Event | None = None,
                retention_days: int = DEFAULT_RETENTION_DAYS) -> None:
    stop_event = stop_event or threading.Event()
    logger.info("celery-manager: sampling queue depths every %gs", interval)
    last_prune: datetime | None = None
    while not stop_event.is_set():
        try:
            sample_once(session_factory, celery_app, interval=interval)
            now = datetime.now(timezone.utc)
            if retention_days > 0 and (last_prune is None or now - last_prune >= SELF_PRUNE_EVERY):
                last_prune = now
                with session_factory() as session:
                    prune_samples(session, retention_days, now=now)
        except Exception:  # e.g. the database is briefly unreachable
            logger.exception("celery-manager: queue depth sampling failed")
        stop_event.wait(interval)


def start_in_background(session_factory, celery_app, config) -> tuple[threading.Thread, threading.Event] | None:
    """Start the sampler thread, or return None when
    `config.queue_sample_interval_seconds` is 0 (disabled)."""
    interval = float(config.queue_sample_interval_seconds)
    if interval <= 0:
        return None
    stop_event = threading.Event()
    thread = threading.Thread(
        target=run_forever,
        args=(session_factory, celery_app, interval, stop_event, config.queue_sample_retention_days),
        name="cm-queue-sampler", daemon=True,
    )
    thread.start()
    return thread, stop_event


# --- retention ----------------------------------------------------------------


def prune_samples(session, retention_days: int | None, *, now: datetime | None = None,
                  dry_run: bool = False, batch_size: int = 5000) -> int:
    """Delete samples older than `retention_days` (None or 0: keep all).
    Returns how many were (or, with `dry_run`, would be) deleted."""
    if not retention_days or retention_days <= 0:
        return 0
    now = _aware(now) if now else datetime.now(timezone.utc)
    cutoff = now - timedelta(days=retention_days)
    condition = QueueDepthSample.sampled_at < cutoff
    if dry_run:
        return session.query(func.count(QueueDepthSample.id)).filter(condition).scalar() or 0
    deleted = 0
    while True:
        ids = [r[0] for r in session.query(QueueDepthSample.id).filter(condition).limit(batch_size).all()]
        if not ids:
            break
        session.execute(delete(QueueDepthSample).where(QueueDepthSample.id.in_(ids)))
        session.commit()
        deleted += len(ids)
        if len(ids) < batch_size:
            break
    return deleted


# --- reading ------------------------------------------------------------------


def history(session, queue: str, hours: float = 24, *, now: datetime | None = None,
            max_points: int = MAX_POINTS) -> dict:
    """The queue's depth over the last `hours`, downsampled.

    The window is cut into equal buckets, as many as fit `max_points` but never
    narrower than a minute; each bucket reports the *highest* depth sampled in
    it (a backlog spike must not be averaged away), stamped with the bucket's
    start. Empty buckets are omitted, so a gap in sampling is a gap in the
    line. Done in Python over at most `ROW_CAP` rows so it is the same on every
    database.
    """
    now = _aware(now) if now else datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    rows = (
        session.query(QueueDepthSample.sampled_at, QueueDepthSample.depth)
        .filter(QueueDepthSample.queue == queue, QueueDepthSample.sampled_at >= since,
                QueueDepthSample.sampled_at <= now)
        .order_by(QueueDepthSample.sampled_at.desc())
        .limit(ROW_CAP)
        .all()
    )
    rows.reverse()
    bucket = max(60.0, hours * 3600.0 / max_points)
    start = since.timestamp()
    buckets: dict[int, int] = {}
    for sampled_at, depth in rows:
        index = int((_aware(sampled_at).timestamp() - start) // bucket)
        buckets[index] = max(depth, buckets.get(index, depth))
    points = [
        {"at": _iso(datetime.fromtimestamp(start + index * bucket, timezone.utc)), "depth": depth}
        for index, depth in sorted(buckets.items())
    ]
    return {
        "queue": queue,
        "hours": hours,
        "bucket_seconds": int(bucket),
        "samples": len(rows),
        "points": points,
    }


def growth(depths: list[int], min_increase: int) -> dict | None:
    """Whether a series of depths (oldest first) is "rising monotonically-ish".

    All of these must hold, else None:

    1. at least `MIN_GROWTH_SAMPLES` (3) samples;
    2. net rise `last - first` of at least `min_increase`;
    3. at most 20% of the steps between consecutive samples go *down*;
    4. no single step down is larger than half the net rise (a queue drained
       by more than half of what it gained mid-window is recovering, not
       growing).

    Flat steps are neutral. So noise and a short dip are tolerated, a queue
    that climbs and then falls back is not, and neither is one that only
    jumped once and stayed (that is a burst; if it persists, the rise still
    counts, which is the point: nothing is draining it). Returns the details
    for the incident.
    """
    if len(depths) < MIN_GROWTH_SAMPLES:
        return None
    rise = depths[-1] - depths[0]
    if rise < min_increase:
        return None
    steps = [b - a for a, b in zip(depths, depths[1:], strict=False)]
    drops = [-s for s in steps if s < 0]
    if len(drops) / len(steps) > MAX_DECREASING_STEP_SHARE:
        return None
    if drops and max(drops) > rise * MAX_DIP_SHARE_OF_RISE:
        return None
    return {"from": depths[0], "to": depths[-1], "increase": rise, "samples": len(depths)}


def window_samples(session, queue: str, since: datetime, now: datetime) -> list[tuple[datetime, int]]:
    rows = (
        session.query(QueueDepthSample.sampled_at, QueueDepthSample.depth)
        .filter(QueueDepthSample.queue == queue, QueueDepthSample.sampled_at >= since,
                QueueDepthSample.sampled_at <= now)
        .order_by(QueueDepthSample.sampled_at)
        .all()
    )
    return [(_aware(at), depth) for at, depth in rows]
