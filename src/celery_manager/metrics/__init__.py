"""Prometheus metrics computed from celery-manager's own database and broker.

Every number here is derived at scrape time from stored rows, not counted in
process memory, because the processes that write runs (workers, the events
listener) are not the process that serves `/metrics`. That shapes the metric
types:

- `celery_manager_task_runs` is a **gauge of stored rows**, not a Prometheus
  counter. It drops when retention prunes rows (and rows change status), so
  `rate()` over it is meaningless. Use the `*_window` metrics for rates of
  work: they describe the last `WINDOW_SECONDS`.
- The runtime "histogram" is likewise windowed, so it is exposed as gauges
  named `..._bucket`/`_sum`/`_count`. `histogram_quantile()` works on them
  (it only needs the `le` label); `rate()` over them does not.

`MetricsCollector` caches the computed snapshot for `metrics_cache_seconds`,
so a scrape storm costs one database pass and one broker round trip, not one
per request. Broker-side metrics (queue depth, workers) are omitted, never
faked, when the broker cannot answer; `celery_manager_collector_up` says
which part failed.

Label cardinality is bounded: task names come from the tasks Celery has
registered plus the names found in the database, capped at `MAX_TASK_LABELS`;
anything beyond goes into `task="other"`.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func

from celery_manager.control.broker import broker_health
from celery_manager.control.latency import wait_samples
from celery_manager.control.missed import missed_run_counts
from celery_manager.control.queues import _run_with_deadline, list_queues
from celery_manager.control.rabbitmq import not_configured_reason as rabbitmq_not_configured
from celery_manager.control.rabbitmq import queue_stats as rabbitmq_queue_stats
from celery_manager.control.tasks import list_registered_tasks
from celery_manager.db.models import TaskRun

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 900  # 15 minutes: failure ratio, finished counts, runtime buckets
MISSED_WINDOW = timedelta(hours=24)
MAX_TASK_LABELS = 200
MAX_SCHEDULE_LABELS = 200
MAX_QUEUE_LABELS = 200
OTHER_TASK = "other"
RUNTIME_BUCKETS = (0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 300.0, 900.0, 3600.0)

# Every metric name a snapshot can contain. Static so the OpenTelemetry gauges
# can be registered once, even if the broker is down at that moment; a test
# keeps it in step with what compute_snapshot emits.
METRIC_NAMES = (
    "celery_manager_window_seconds",
    "celery_manager_task_runs",
    "celery_manager_task_runs_finished_window",
    "celery_manager_task_failure_ratio_window",
    "celery_manager_task_runtime_window_seconds_bucket",
    "celery_manager_task_runtime_window_seconds_sum",
    "celery_manager_task_runtime_window_seconds_count",
    "celery_manager_queue_depth",
    "celery_manager_queue_recorded_queued",
    "celery_manager_queue_wait_seconds",
    "celery_manager_workers_online",
    "celery_manager_schedule_missed_runs_24h",
    "celery_manager_broker_up",
    "celery_manager_broker_latency_seconds",
    "celery_manager_broker_memory_used_bytes",
    "celery_manager_broker_memory_max_bytes",
    "celery_manager_broker_evicted_keys",
    "celery_manager_broker_connected_clients",
    "celery_manager_collector_up",
    # RabbitMQ management API (only when rabbitmq_management_url is set).
    "celery_manager_rabbitmq_queue_unacked",
    "celery_manager_rabbitmq_queue_consumers",
    "celery_manager_rabbitmq_queue_publish_rate",
    "celery_manager_rabbitmq_queue_deliver_rate",
    "celery_manager_rabbitmq_mem_alarm",
    "celery_manager_rabbitmq_disk_alarm",
)

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


@dataclass
class Family:
    """One metric name with its help text and samples. Always a gauge: see the
    module docstring for why nothing here is a counter."""

    name: str
    help: str
    samples: list[tuple[dict[str, str], float]] = field(default_factory=list)
    type: str = "gauge"


# --- text exposition ---------------------------------------------------------


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _format_value(value: float) -> str:
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "+Inf" if value > 0 else "-Inf"
        return repr(value)
    return str(value)


def render(families: list[Family]) -> str:
    """Prometheus text format 0.0.4. Families with no samples are skipped, so
    an omitted metric leaves no orphan HELP/TYPE lines."""
    lines: list[str] = []
    for family in families:
        if not family.samples:
            continue
        help_text = family.help.replace("\\", "\\\\").replace("\n", "\\n")
        lines.append(f"# HELP {family.name} {help_text}")
        lines.append(f"# TYPE {family.name} {family.type}")
        for labels, value in family.samples:
            if labels:
                rendered = ",".join(f'{k}="{_escape_label(str(v))}"' for k, v in labels.items())
                lines.append(f"{family.name}{{{rendered}}} {_format_value(value)}")
            else:
                lines.append(f"{family.name} {_format_value(value)}")
    return "\n".join(lines) + "\n"


# --- computing the snapshot -------------------------------------------------


def _task_labeller(session, celery_app):
    """Maps a stored task name to a bounded label value."""
    registered: list[str] = []
    try:
        registered = [t["name"] for t in list_registered_tasks(celery_app)]
    except Exception:
        logger.debug("metrics: could not list registered tasks", exc_info=True)
    known = list(dict.fromkeys(sorted(registered)))[:MAX_TASK_LABELS]
    room = MAX_TASK_LABELS - len(known)
    if room > 0:
        # Names the database has seen, busiest first, fill what the registry left.
        recorded = (
            session.query(TaskRun.task_name, func.count(TaskRun.id).label("n"))
            .group_by(TaskRun.task_name)
            .order_by(func.count(TaskRun.id).desc())
            .limit(MAX_TASK_LABELS)
            .all()
        )
        for name, _ in recorded:
            if name not in known and room > 0:
                known.append(name)
                room -= 1
    allowed = set(known)
    return lambda name: name if name in allowed else OTHER_TASK


def _run_families(session, celery_app, now: datetime) -> list[Family]:
    label = _task_labeller(session, celery_app)
    since = now - timedelta(seconds=WINDOW_SECONDS)

    stored: dict[tuple[str, str], int] = defaultdict(int)
    for name, status, n in session.query(TaskRun.task_name, TaskRun.status, func.count(TaskRun.id)).group_by(
        TaskRun.task_name, TaskRun.status
    ):
        stored[(label(name), status)] += n

    finished: dict[tuple[str, str], int] = defaultdict(int)
    for name, status, n in (
        session.query(TaskRun.task_name, TaskRun.status, func.count(TaskRun.id))
        .filter(TaskRun.finished_at >= since, TaskRun.finished_at <= now)
        .group_by(TaskRun.task_name, TaskRun.status)
    ):
        finished[(label(name), status)] += n

    # Cumulative bucket counts, summed per task in one pass over the window.
    bucket_cols = [
        func.sum(case((TaskRun.runtime_seconds <= le, 1), else_=0)) for le in RUNTIME_BUCKETS
    ]
    hist: dict[str, list[float]] = {}  # task -> [bucket counts..., sum, count]
    for name, total, count, *buckets in (
        session.query(
            TaskRun.task_name,
            func.sum(TaskRun.runtime_seconds),
            func.count(TaskRun.id),
            *bucket_cols,
        )
        .filter(
            TaskRun.finished_at >= since,
            TaskRun.finished_at <= now,
            TaskRun.runtime_seconds.isnot(None),
        )
        .group_by(TaskRun.task_name)
    ):
        row = hist.setdefault(label(name), [0.0] * (len(RUNTIME_BUCKETS) + 2))
        for i, value in enumerate(buckets):
            row[i] += float(value or 0)
        row[-2] += float(total or 0)
        row[-1] += count

    runs = Family(
        "celery_manager_task_runs",
        "Task runs currently stored, by task and status. A gauge of stored rows, not a counter: "
        "it falls when retention prunes.",
    )
    runs.samples = [({"task": t, "status": s}, n) for (t, s), n in sorted(stored.items())]

    window = Family(
        "celery_manager_task_runs_finished_window",
        "Runs that finished in the last window (see celery_manager_window_seconds), by task and status.",
    )
    window.samples = [({"task": t, "status": s}, n) for (t, s), n in sorted(finished.items())]

    ratio = Family(
        "celery_manager_task_failure_ratio_window",
        "failed / (succeeded + failed) among runs that finished in the last window. "
        "Absent for a task with no finished runs in it.",
    )
    per_task: dict[str, dict[str, int]] = defaultdict(lambda: {"failed": 0, "succeeded": 0})
    for (t, s), n in finished.items():
        if s in ("failed", "succeeded"):
            per_task[t][s] += n
    for t, counts in sorted(per_task.items()):
        total = counts["failed"] + counts["succeeded"]
        if total:
            ratio.samples.append(({"task": t}, counts["failed"] / total))

    bucket = Family(
        "celery_manager_task_runtime_window_seconds_bucket",
        "Runs finished in the last window with a runtime at most `le` seconds (cumulative). "
        "Windowed gauge: use histogram_quantile(), not rate().",
    )
    total_f = Family("celery_manager_task_runtime_window_seconds_sum", "Sum of runtimes of runs finished in the last window.")
    count_f = Family("celery_manager_task_runtime_window_seconds_count", "Runs with a runtime that finished in the last window.")
    for t in sorted(hist):
        row = hist[t]
        for le, value in zip(RUNTIME_BUCKETS, row, strict=False):
            bucket.samples.append(({"task": t, "le": repr(le)}, value))
        bucket.samples.append(({"task": t, "le": "+Inf"}, row[-1]))
        total_f.samples.append(({"task": t}, row[-2]))
        count_f.samples.append(({"task": t}, row[-1]))

    return [runs, window, ratio, bucket, total_f, count_f]


def _queue_families(session, celery_app) -> tuple[list[Family], bool]:
    listing = list_queues(celery_app, session, include_workers=False)
    depth = Family("celery_manager_queue_depth", "Messages waiting in the broker, per queue. Absent when the broker cannot say.")
    recorded = Family("celery_manager_queue_recorded_queued", "Runs recorded as queued, per queue (from the database, not the broker).")
    for q in listing["queues"][:MAX_QUEUE_LABELS]:
        recorded.samples.append(({"queue": q["name"]}, q["recorded_queued"]))
        if q["depth"] is not None:
            depth.samples.append(({"queue": q["name"]}, q["depth"]))
    # The broker side counts as up only if it answered for at least one queue,
    # or there was nothing to ask about.
    broker_ok = listing["broker"]["error"] is None
    return [depth, recorded], broker_ok


def _queue_wait_family(session, now: datetime) -> Family:
    """Queue wait (started_at - max(sent_at, eta)) quantiles per queue over the
    last window. Gauges, like the runtime histogram: a windowed snapshot, so
    they move up and down and `rate()` over them is meaningless. Queues with no
    qualifying run in the window have no sample."""
    family = Family(
        "celery_manager_queue_wait_seconds",
        "Seconds runs waited in the queue before starting (ETA/countdown time excluded), over the last window. "
        "quantile is 0.5, 0.95 or 1 (the maximum). Absent for a queue with no timed run in the window.",
    )
    stats = wait_samples(session, now=now, window=timedelta(seconds=WINDOW_SECONDS)).stats()
    for queue, entry in list(stats["by_queue"].items())[:MAX_QUEUE_LABELS]:
        for quantile, key in (("0.5", "p50"), ("0.95", "p95"), ("1", "max")):
            family.samples.append(({"queue": queue, "quantile": quantile}, entry[key]))
    return family


def _workers_family(celery_app, timeout: float) -> Family:
    # An unreachable broker makes kombu retry for several seconds, far past
    # the inspect timeout; bound it the way the queue probe is bounded so a
    # scrape never hangs on a dead broker.
    pings = _run_with_deadline(lambda: celery_app.control.inspect(timeout=timeout).ping()) or {}
    family = Family("celery_manager_workers_online", "Workers that answered a ping within the inspect timeout.")
    family.samples = [({}, len(pings))]
    return family


def _missed_family(session, celery_app, grace_seconds: int, now: datetime) -> Family:
    counts = missed_run_counts(session, celery_app, now - MISSED_WINDOW, grace_seconds=grace_seconds, now=now)
    family = Family("celery_manager_schedule_missed_runs_24h", "Scheduled firings that never arrived in the last 24 hours, per beat schedule.")
    names = sorted(celery_app.conf.beat_schedule or {})[:MAX_SCHEDULE_LABELS]
    family.samples = [({"schedule": n}, counts.get(n, 0)) for n in names]
    return family


def _broker_families(celery_app) -> list[Family]:
    """Broker reachability for every transport; memory, evictions and clients
    for Redis only. `broker_up` is the signal for an outage (0, with the
    latency and Redis metrics omitted), so a down broker does not trip
    `collector_up`: the collector did its job and the answer was "down"."""
    health = broker_health(celery_app)
    transport = {"transport": health["transport"] or "unknown"}
    up = Family("celery_manager_broker_up", "1 if the broker answered a connection, 0 if it did not.")
    up.samples = [(transport, 1 if health["reachable"] else 0)]
    latency = Family("celery_manager_broker_latency_seconds", "Time to connect to the broker (and ping it, on Redis).")
    used = Family("celery_manager_broker_memory_used_bytes", "Redis used_memory. Redis only.")
    limit = Family("celery_manager_broker_memory_max_bytes", "Redis maxmemory; 0 means no limit. Redis only.")
    evicted = Family("celery_manager_broker_evicted_keys", "Keys Redis has evicted since it started. Redis only; nonzero can mean lost tasks.")
    clients = Family("celery_manager_broker_connected_clients", "Clients connected to Redis. Redis only.")
    if health["reachable"]:
        latency.samples = [(transport, health["latency_ms"] / 1000)]
        details = health["details"] or {}
        if health["transport"] == "redis":
            for family, key in ((used, "used_memory"), (limit, "maxmemory"), (evicted, "evicted_keys"),
                                (clients, "connected_clients")):
                if details.get(key) is not None:
                    family.samples = [(transport, details[key])]
    return [up, latency, used, limit, evicted, clients]
def _rabbitmq_families(config) -> list[Family]:
    """Per-queue and per-node numbers from the RabbitMQ management API.

    Every queue in the broker's vhost (capped at MAX_QUEUE_LABELS), not only
    the ones celery-manager knows: a dead-letter queue filling up is exactly
    the queue nobody configured. A rate the management API hasn't sampled
    yet is left out rather than reported as 0.
    """
    queues, nodes = rabbitmq_queue_stats(config)
    unacked = Family("celery_manager_rabbitmq_queue_unacked", "Messages delivered to consumers but not yet acknowledged, per RabbitMQ queue.")
    consumers = Family("celery_manager_rabbitmq_queue_consumers", "Consumers attached, per RabbitMQ queue.")
    publish = Family("celery_manager_rabbitmq_queue_publish_rate", "Messages published per second into the queue, as sampled by the management API.")
    deliver = Family("celery_manager_rabbitmq_queue_deliver_rate", "Messages delivered (or fetched) per second from the queue, as sampled by the management API.")
    for q in sorted(queues, key=lambda q: q["name"])[:MAX_QUEUE_LABELS]:
        labels = {"queue": q["name"]}
        for family, key in ((unacked, "messages_unacknowledged"), (consumers, "consumers"),
                            (publish, "publish_rate"), (deliver, "deliver_rate")):
            if isinstance(q.get(key), (int, float)):
                family.samples.append((labels, q[key]))
    mem = Family("celery_manager_rabbitmq_mem_alarm", "1 while the RabbitMQ node has a memory alarm (publishers are blocked).")
    disk = Family("celery_manager_rabbitmq_disk_alarm", "1 while the RabbitMQ node has a disk-space alarm (publishers are blocked).")
    for node in nodes:
        if node.get("name"):
            mem.samples.append(({"node": node["name"]}, 1 if node.get("mem_alarm") else 0))
            disk.samples.append(({"node": node["name"]}, 1 if node.get("disk_free_alarm") else 0))
    return [unacked, consumers, publish, deliver, mem, disk]


def compute_snapshot(config, session_factory, now: datetime | None = None) -> list[Family]:
    """One pass over the database and (briefly) the broker. Each source that
    fails is left out and flagged in `celery_manager_collector_up`; a broker
    outage degrades the scrape, it does not fail it."""
    now = now or datetime.now(timezone.utc)
    celery_app = config.celery_app
    families: list[Family] = []
    up = Family("celery_manager_collector_up", "1 if that part of the snapshot was collected, 0 if it failed and its metrics are omitted.")
    families.append(Family("celery_manager_window_seconds", "Length of the window the *_window metrics cover.", [({}, float(WINDOW_SECONDS))]))

    def attempt(collector: str, fn):
        try:
            result = fn()
        except Exception:
            logger.warning("metrics: collector %r failed", collector, exc_info=True)
            up.samples.append(({"collector": collector}, 0))
            return None
        up.samples.append(({"collector": collector}, 1))
        return result

    with session_factory() as session:
        runs = attempt("runs", lambda: _run_families(session, celery_app, now))
        families.extend(runs or [])

        try:
            queue_families, broker_ok = _queue_families(session, celery_app)
        except Exception:
            logger.warning("metrics: collector 'queues' failed", exc_info=True)
            queue_families, broker_ok = [], False
        # list_queues reports broker trouble instead of raising: depths are
        # simply absent then. The recorded-queued counts are database facts
        # and stay either way.
        up.samples.append(({"collector": "queues"}, 1 if broker_ok else 0))
        families.extend(queue_families)

        queue_wait = attempt("queue_wait", lambda: _queue_wait_family(session, now))
        if queue_wait is not None:
            families.append(queue_wait)

        workers = attempt("workers", lambda: _workers_family(celery_app, config.inspect_timeout_seconds))
        if workers is not None:
            families.append(workers)

        missed = attempt("missed", lambda: _missed_family(session, celery_app, config.missed_run_grace_seconds, now))
        if missed is not None:
            families.append(missed)

        broker = attempt("broker", lambda: _broker_families(celery_app))
        if broker is not None:
            families.extend(broker)

    # Only asked for (and only reported in collector_up) when configured: a
    # Redis deployment shouldn't carry a permanently failing collector.
    if getattr(config, "rabbitmq_management_url", None) and not rabbitmq_not_configured(config):
        rabbit = attempt("rabbitmq", lambda: _rabbitmq_families(config))
        families.extend(rabbit or [])

    families.append(up)
    return families


class MetricsCollector:
    """Computes the snapshot and holds it for `cache_seconds`. Thread-safe: a
    scrape that arrives while another is computing waits for it and reuses its
    result instead of starting a second pass."""

    def __init__(self, config, session_factory, clock=time.monotonic):
        self._config = config
        self._session_factory = session_factory
        self._clock = clock
        self._lock = threading.Lock()
        self._cached: tuple[float, list[Family]] | None = None

    def snapshot(self) -> list[Family]:
        ttl = max(0.0, float(self._config.metrics_cache_seconds))
        with self._lock:
            now = self._clock()
            if self._cached is not None and ttl > 0 and now - self._cached[0] < ttl:
                return self._cached[1]
            families = compute_snapshot(self._config, self._session_factory)
            # Stamped after computing, so a slow collection doesn't expire its own result.
            self._cached = (self._clock(), families)
            return families

    def render(self) -> str:
        return render(self.snapshot())
