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
| `celery_manager_queue_wait_seconds` | `queue`, `quantile` | How long runs that started in the last 15 minutes waited in the queue, excluding ETA/countdown time ([how](queues.md#wait-time)). `quantile` is `0.5`, `0.95` or `1` (the maximum). A windowed gauge, like the runtime buckets; absent for a queue with no run that has a recorded send time. |
| `celery_manager_workers_online` | none | Workers that answered a ping within `inspect_timeout_seconds`. Omitted if the broker cannot be reached. |
| `celery_manager_schedule_missed_runs_24h` | `schedule` | Scheduled firings that never arrived in the last 24 hours (needs the [stamping beat scheduler](decorator.md#missed-run-detection)). |
| `celery_manager_broker_up` | `transport` | `1` if the broker accepted a connection, `0` if not. An outage shows here, not in `collector_up`. |
| `celery_manager_broker_latency_seconds` | `transport` | Time to connect to the broker (and ping it, on Redis). Omitted while the broker is down. |
| `celery_manager_broker_memory_used_bytes` | `transport` | Redis `used_memory`. Redis only. |
| `celery_manager_broker_memory_max_bytes` | `transport` | Redis `maxmemory`; `0` means no limit. Redis only. |
| `celery_manager_broker_evicted_keys` | `transport` | Keys Redis has evicted since it started. Redis only. Above zero can mean queued tasks were lost (see [Broker health](queues.md#broker-health)). |
| `celery_manager_broker_connected_clients` | `transport` | Clients connected to Redis. Redis only. |
| `celery_manager_rabbitmq_queue_unacked` | `queue` | Messages delivered to consumers but not yet acknowledged. Every queue in the vhost (at most 200). Only with [RabbitMQ management](rabbitmq.md) configured, as are the next five. |
| `celery_manager_rabbitmq_queue_consumers` | `queue` | Consumers attached to the queue. |
| `celery_manager_rabbitmq_queue_publish_rate` | `queue` | Messages published into the queue per second, as sampled by the management API. Absent until it has a sample. |
| `celery_manager_rabbitmq_queue_deliver_rate` | `queue` | Messages delivered or fetched from the queue per second. Absent until sampled. |
| `celery_manager_rabbitmq_mem_alarm` | `node` | `1` while the node has a memory alarm (RabbitMQ blocks every publisher), else `0`. |
| `celery_manager_rabbitmq_disk_alarm` | `node` | `1` while the node has a disk-space alarm, else `0`. |
| `celery_manager_window_seconds` | none | The length of the window above: 900. |
| `celery_manager_collector_up` | `collector` | `1` or `0` for `runs`, `queues`, `queue_wait`, `workers`, `missed`, `broker`, and `rabbitmq` when RabbitMQ management is configured. `0` means that part failed and its metrics are left out of the response. |

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

## Grafana dashboard and alert rules

Two ready-made files ship in the repository (and the sdist) under
`examples/monitoring/`:

- `grafana-dashboard.json`: import it in Grafana (**Dashboards → New →
  Import**) and pick your Prometheus data source. Panels: broker up, workers
  online, backlog, failing collectors, failure ratio and p95 runtime per
  task, finished runs by status, queue depth and wait, missed scheduled runs,
  broker latency and Redis memory.
- `prometheus-rules.yml`: ten alerting rules (collector down, broker down, no
  workers, high failure ratio with a minimum of 10 runs, slow p95, backlog,
  queue wait, missed schedule firings, Redis evictions, RabbitMQ resource
  alarms). Add it under `rule_files:`; it passes `promtool check rules`.

The thresholds are starting points. Both files are checked in CI against the
metric names this exporter really emits, so they do not drift from it.

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


## Alongside an event exporter

The package's metrics summarize stored observations and manager-specific state.
For continuously accumulating event counters and a wider Grafana/Prometheus
alerting stack, deploy an independent Celery event exporter alongside it.
Keep exporter metric names and collection semantics separate from these
windowed gauges; do not apply `rate()` to a DB-derived windowed gauge.
Configure events on workers and authenticate network access to both scrape
endpoints. Existing Prometheus/Alertmanager deployments can remain the
notification authority while the dashboard supplies run links and manager
controls. The manager does not install or operate that external stack.
