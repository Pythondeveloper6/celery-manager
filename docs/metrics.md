---
title: Metrics
---

# Prometheus and OpenTelemetry metrics

celery-manager serves run counts, durations, failure ratios, queue depth,
online workers and missed runs at `GET /metrics`, in the Prometheus text
format. For teams that already watch Grafana.

## Turn it on

The endpoint is **off by default** and answers `404` until you set a token:

```python
config = CeleryManagerConfig(
    celery_app=app,
    db_url="...",
    secret_key="...",
    metrics_token="<a long random value>",   # python -c "import secrets; print(secrets.token_urlsafe(32))"
    metrics_cache_seconds=5.0,               # default
)
```

Scrapers send `Authorization: Bearer <token>`. A missing or wrong token is a
`401`. Prometheus cannot use the dashboard's session cookie, so the token is
the only credential: treat it like a password and serve `/metrics` over
HTTPS. It is a read-only `GET`, so no role applies. When the dashboard is
mounted under a prefix, so is the endpoint (`/celery-manager/metrics`).

## Prometheus scrape config

```yaml
scrape_configs:
  - job_name: celery-manager
    metrics_path: /metrics            # or /celery-manager/metrics
    scheme: https
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/celery-manager.token
    scrape_interval: 30s
    scrape_timeout: 10s
    static_configs:
      - targets: ["dashboard.example.com"]
```

## What you get

Everything is computed from celery-manager's database and the broker at
scrape time, and every metric is a gauge.

| Metric | Labels | Meaning |
|---|---|---|
| `celery_manager_task_runs` | `task`, `status` | Runs **currently stored**. A gauge of rows, not a counter: it falls when [retention](retention.md) prunes and when a run changes status, so do not `rate()` it. |
| `celery_manager_task_runs_finished_window` | `task`, `status` | Runs that finished in the last 15 minutes. |
| `celery_manager_task_failure_ratio_window` | `task` | failed / (succeeded + failed) over the last 15 minutes. Absent for a task with nothing finished in it. |
| `celery_manager_task_runtime_window_seconds_bucket` | `task`, `le` | Runtime buckets for runs finished in the last 15 minutes (cumulative). |
| `celery_manager_task_runtime_window_seconds_sum` / `_count` | `task` | Total runtime and number of runs behind those buckets. |
| `celery_manager_queue_depth` | `queue` | Messages waiting in the broker. Omitted when the broker cannot say (a down broker, or `memory://`). |
| `celery_manager_queue_recorded_queued` | `queue` | Runs recorded as `queued` (from the database). |
| `celery_manager_workers_online` | none | Workers that answered a ping within `inspect_timeout_seconds`. Omitted if the broker cannot be reached. |
| `celery_manager_schedule_missed_runs_24h` | `schedule` | Scheduled firings that never arrived in the last 24 hours (needs the [stamping beat scheduler](decorator.md#missed-run-detection)). |
| `celery_manager_broker_up` | `transport` | `1` if the broker accepted a connection, `0` if not. An outage shows here, not in `collector_up`. |
| `celery_manager_broker_latency_seconds` | `transport` | Time to connect to the broker (and ping it, on Redis). Omitted while the broker is down. |
| `celery_manager_broker_memory_used_bytes` | `transport` | Redis `used_memory`. Redis only. |
| `celery_manager_broker_memory_max_bytes` | `transport` | Redis `maxmemory`; `0` means no limit. Redis only. |
| `celery_manager_broker_evicted_keys` | `transport` | Keys Redis has evicted since it started. Redis only. Above zero can mean queued tasks were lost (see [Broker health](queues.md#broker-health)). |
| `celery_manager_broker_connected_clients` | `transport` | Clients connected to Redis. Redis only. |
| `celery_manager_window_seconds` | none | The length of the window above: 900. |
| `celery_manager_collector_up` | `collector` | `1` or `0` for `runs`, `queues`, `workers`, `missed`, `broker`. `0` means that part failed and its metrics are left out of the response. |

The "histogram" is windowed, so it is exposed as gauges named
`_bucket`/`_sum`/`_count`, not as a Prometheus histogram. Use
`histogram_quantile` on it; `rate()` does not apply.

**Label cardinality is bounded.** `task` is the name of a task Celery has
registered or celery-manager has recorded, at most 200 of them; the rest are
summed into `task="other"`. Queue and schedule labels are capped at 200 too.

**Caching.** The computed snapshot is reused for `metrics_cache_seconds`
(default 5), so several scrapers, or a scrape storm, cost one database pass
and one broker round trip per interval. Set it to `0` to compute on every
request. If the broker is unreachable, a scrape still succeeds and takes up to
about three seconds per broker call; queue depth and worker count are simply
missing.

## Grafana tips

```promql
# Failure ratio per task, worst first
topk(10, celery_manager_task_failure_ratio_window)

# p95 runtime per task, over the last 15 minutes
histogram_quantile(0.95, sum by (task, le) (celery_manager_task_runtime_window_seconds_bucket))

# Backlog
sum(celery_manager_queue_depth)

# Alert: no workers
celery_manager_workers_online == 0

# Alert: a periodic task is not firing
celery_manager_schedule_missed_runs_24h > 0
```

`celery_manager_collector_up == 0` is worth an alert of its own: it is how
you tell a missing metric from a healthy zero.

## OpenTelemetry

Install the extra, then register the same numbers as observable gauges:

```bash
pip install "celery-manager[otel]"
```

```python
from celery_manager.api.app import create_app
from celery_manager.metrics import otel

app, session_factory = create_app(config)
otel.register(meter_provider, app=app)   # meter_provider: your SDK MeterProvider
                                         # (omit it to use the global one)
```

Each metric above becomes an observable gauge of the same name, read from the
same cached snapshot every time your exporter collects. Only the API is
needed to import the module; configure the SDK and exporter yourself. Without
`opentelemetry-api`, `register` raises an `ImportError` that says which extra
to install. The OpenTelemetry path does not need `metrics_token`.
