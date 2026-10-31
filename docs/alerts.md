---
title: Alerts
---

# Alerts

Alerts send a message to Slack, a webhook or email when something goes wrong:

- a task's **failure rate** goes over a threshold,
- a **queue backs up**,
- a **worker goes offline**,
- a periodic task **misses its run**.

You set up **rules** (what to watch) and **channels** (where messages go) on
the **Alerts** page. A separate process, the **evaluator**, checks the rules
every minute and sends the messages. Each problem is an **incident**: it sends
one message when it starts and, if you want, one when it is over.

## Run the evaluator

Rules are only checked while an evaluator is running. Start one next to the
events listener:

```bash
celery-manager alerts --celery-app your_module:your_celery_app --db-url postgresql://...
```

Or run it inside the events listener process, on its own thread:

```bash
celery-manager events --celery-app your_module:your_celery_app --db-url postgresql://... --with-alerts
```

Options:

- `--interval 60`: seconds between checks (default `alert_interval_seconds`, 60).
- `--once`: check every rule once, print what was sent, and exit. Handy in a
  cron job or to try a rule out.
- `--config your_module:config`: import a whole `CeleryManagerConfig` instead
  of building one from `--celery-app` and `--db-url`. This is how the
  evaluator gets the [SMTP settings](#email) and `dashboard_url`, which have
  no command-line flags (a password on the command line is visible to every
  user on the machine in `ps`). `--celery-app` and `--db-url` given as well
  override the config's values.

From Python, `celery_manager.alerts.run_forever(session_factory, celery_app,
config)` runs the same loop, and `start_in_background(...)` runs it on a
daemon thread; `evaluate_once(...)` is one check.

The Alerts page warns when a rule has never been checked, which usually means
no evaluator is running.

### Running more than one

You can run several evaluators against the same database, for example one per
events listener, for redundancy. They don't send duplicate messages: every
decision that leads to a message is a database write only one process can win
(a unique key on the open incident, and a compare-and-set update before each
message). Nothing that decides a message is kept in memory between checks, so restarting an evaluator
loses nothing (the one exception, `broker_unreachable`'s down-since time, only delays its alert).

## Rule kinds

Every rule has a **target**: a task, queue, worker or schedule name, a
pattern such as `reports.*` or `celery@web-*`, or `*` for all. Each name that
matches is judged on its own and gets its own incident, so one rule on `*` can
tell you about three failing tasks separately.

| Kind | Target | Fires when | Parameters (default) |
|---|---|---|---|
| `failure_rate` | task name | At least `min_runs` runs of the task finished in the last `window_minutes`, and at least `rate` of them failed. Only finished runs count (succeeded and failed), as on the Insights page. | `rate` (0.2, a fraction: 0.2 is 20%), `min_runs` (10), `window_minutes` (15) |
| `queue_backlog` | queue name | The broker holds at least `depth` messages in the queue. | `depth` (1000) |
| `worker_offline` | worker name | A worker the evaluator has seen before hasn't answered a ping for `minutes`. | `minutes` (5) |
| `missed_run` | schedule name | A run of the schedule that was due in the last `window_minutes` never arrived. | `window_minutes` (60) |
| `broker_unreachable` | `*` (one broker) | The broker has not accepted a connection for `minutes`. | `minutes` (2) |
| `broker_memory` | `*` (one broker) | Redis uses at least `ratio` of its `maxmemory`. Redis only. | `ratio` (0.8, a fraction: 0.8 is 80%) |
| `queue_wait` | queue name | At least `min_runs` runs started from the queue in the last `window_minutes`, and their `quantile`-th percentile queue wait is at least `seconds`. | `seconds` (60), `quantile` (95, meaning p95; 1 to 100), `window_minutes` (15), `min_runs` (5) |
| `queue_growing` | queue name | The queue's sampled depth rose by at least `min_increase` over the last `minutes` without draining on the way. | `minutes` (15), `min_increase` (50) |
| `rabbitmq_alarm` | RabbitMQ node name, e.g. `rabbit@node-1` | A RabbitMQ node reports a memory or disk alarm. Needs [RabbitMQ management](rabbitmq.md). | none |

An incident **resolves** when its condition no longer holds: the failures
leave the window, the queue drains, the worker answers again, or the missed
run is older than the window.

Some details per kind:

- **`failure_rate`** needs `min_runs` so one failure out of one run (100%)
  doesn't wake anyone up. A task that stops running resolves once its failures
  leave the window.
- **`queue_backlog`** reads the broker's own count, the same number as the
  [Queues page](queues.md), with the same limits: brokers that can't report a
  queue's depth (see [Queues](queues.md)) never fire. If the broker can't be
  reached the rule is marked *couldn't check* and nothing opens or resolves
  until it can.
- **`worker_offline`** remembers every worker that has answered a ping
  while a `worker_offline` rule existed. A worker that hasn't answered for 24
  hours is forgotten, which resolves its incident: it was probably scaled
  down or renamed on purpose. If the ping itself fails (broker down), the rule
  is marked *couldn't check* instead of reporting every worker offline. Uses
  `inspect_timeout_seconds` as the ping timeout.
- **`queue_wait`** measures `started_at - max(sent_at, eta)`, so ETA and
  countdown time never counts, and only runs whose send time was recorded
  (see [Wait time](queues.md#wait-time)). `min_runs` keeps three slow runs
  from paging anyone. It resolves when the slow runs leave the window.
- **`queue_growing`** reads the [depth samples](queues.md#depth-history) the
  events listener records, so it needs the listener running with
  `queue_sample_interval_seconds` above 0; the exact growth rule is defined
  there. Gaps in sampling neither fire nor resolve it.
- **`rabbitmq_alarm`** reads `/api/nodes` from the RabbitMQ management API.
  While a node has an alarm RabbitMQ blocks every publisher, so no task can
  be sent. Each node is its own incident; it resolves when the alarm clears.
  If the management API isn't configured or can't be reached the rule is
  marked *couldn't check*. The message names the alarm (memory, disk or both).
- **`missed_run`** uses the same detection as the Schedules page, so it needs
  beat to run the stamping scheduler and respects `missed_run_grace_seconds`
  (see [Missed-run detection](decorator.md#missed-run-detection)).

- **`broker_unreachable`** checks the same way as the
  [Broker panel](queues.md#broker-health) (one connection, 3s deadline). The
  target is always `*`: there is one broker, and any other target is replaced
  with `*`. To know *how long* the broker has been down, the evaluator keeps
  the time it first saw it down **in memory**, so unlike the other kinds a
  restart of the evaluator restarts that clock (a down broker alerts up to
  `minutes` late, never early). The first successful check resolves it.
- **`broker_memory`** reads Redis `used_memory` against `maxmemory`. **It
  never fires when `maxmemory` is 0** (Redis's default: no limit), because
  there is no ceiling to be near; set a `maxmemory` if you want this alert.
  On a broker that isn't Redis the rule is marked *couldn't check* with the
  reason, and an unreachable broker does the same (use `broker_unreachable`
  for that).

## Quiet period, and what gets sent

- **One incident, one message.** A rule sends a message when an incident
  opens. While the problem lasts, nothing more is sent.
- **Resolved message.** When the incident closes, the rule sends a
  "resolved" message, unless you turned **Also send a message when it
  resolves** off.
- **Quiet period** (default 60 minutes). If the same problem comes back
  within the quiet period after its last message, the new incident is
  recorded but not announced. If it is still going when the quiet period
  ends, the message is sent then; if it clears first, nobody is told, not
  even that it resolved. This keeps a flapping queue from sending a message
  every minute.
- Disabling a rule, or changing its kind or target, closes its open incidents
  without a message.

The Alerts page lists recent incidents, with "Not sent yet" on an incident the
quiet period is holding back.

## Channels

Every rule sends to any number of channels. A rule with no channels still
shows its state on the Alerts page.

Each delivery has a time limit (`alert_send_timeout_seconds`, 10 seconds). A
channel that fails or times out doesn't stop the others; its error is shown
on the channel on the Alerts page. If *every* channel of a rule fails, the
message is tried again on the next check; if only some fail, it isn't (that
would send it twice to the ones that worked).

**Send test** on a channel sends a test message straight away and shows the
channel's answer.

### Slack

Create an [incoming webhook](https://api.slack.com/messaging/webhooks) in
Slack and paste its URL. Messages have a one-line summary, the details, and an
**Open in the dashboard** link when `dashboard_url` is set.

### Webhook

Any URL that accepts a JSON `POST`. The body:

```json
{
  "schema": 1,
  "event": "alert.firing",
  "summary": "reports.build: 6 of 10 runs failed in the last 15 min (60%)",
  "rule": {"id": 3, "name": "Report failures", "kind": "failure_rate", "target": "reports.*"},
  "key": "reports.build",
  "details": {"failed": 6, "runs": 10, "failure_rate": 0.6, "threshold": 0.5, "window_minutes": 15},
  "opened_at": "2026-10-28T12:00:00+00:00",
  "resolved_at": null,
  "url": "https://ops.example.com/celery/#/task-types/reports.build",
  "sent_at": "2026-10-28T12:00:00+00:00"
}
```

- `event` is `alert.firing`, `alert.resolved` or `alert.test` (a test has
  `rule` and `key` set to `null`).
- `key` is the task, queue, worker or schedule name (`broker` for the broker kinds).
- `details` depends on the kind: `failed`, `runs`, `failure_rate`,
  `threshold`, `window_minutes` for `failure_rate`; `depth`, `threshold` for
  `queue_backlog`; `last_seen_at`, `offline_minutes`, `threshold_minutes` for
  `worker_offline`; `missed`, `last_due_at`, `window_minutes` for
  `missed_run`; `unreachable_minutes`, `threshold_minutes`, `error` for
  `broker_unreachable`; `used_memory`, `maxmemory`, `ratio`, `threshold` for
  `broker_memory`; `wait_seconds`, `quantile`, `runs`, `threshold`,
  `window_minutes` for `queue_wait`; `from`, `to`, `increase`, `samples`,
  `minutes`, `threshold` for `queue_growing`.
- `url` is `null` without `dashboard_url`.

Any `2xx` answer counts as delivered. Redirects are not followed and count as
a failure.

**Signing.** Give the channel a secret and every request carries two headers:

- `X-Celery-Manager-Timestamp`: Unix time in seconds.
- `X-Celery-Manager-Signature`: `sha256=` and the hex HMAC-SHA256 of
  `"<timestamp>." + body`, keyed with the secret.

To verify, recompute it over the raw body and compare in constant time, and
reject old timestamps so a captured request can't be replayed:

```python
import hashlib, hmac, time

def verify(secret: str, headers, body: bytes, max_age: int = 300) -> bool:
    timestamp = headers["X-Celery-Manager-Timestamp"]
    if abs(time.time() - int(timestamp)) > max_age:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, headers["X-Celery-Manager-Signature"])
```

### Email

An email channel is a list of addresses (up to 20). The mail server is set on
`CeleryManagerConfig`, not in the dashboard:

```python
config = CeleryManagerConfig(
    celery_app=app,
    db_url="postgresql://...",
    dashboard_url="https://ops.example.com/celery/",
    smtp_host="smtp.example.com",
    smtp_port=587,
    smtp_username="alerts@example.com",
    smtp_password=os.environ["SMTP_PASSWORD"],
    smtp_use_tls=True,            # STARTTLS
    alert_email_from="alerts@example.com",
)
```

Give the evaluator this config with `--config`, and the dashboard the same
settings if you want **Send test** to work for email (the test is sent from
the API process). Without `smtp_host`, email channels fail with a message
saying so.

### PagerDuty

A PagerDuty channel sends Events API v2 events. Give it the **integration
key** of a PagerDuty service (Service → Integrations → Events API v2). The URL
defaults to `https://events.pagerduty.com/v2/enqueue`; set it only for the EU
endpoint (`https://events.eu.pagerduty.com/v2/enqueue`) or a proxy. The key is
stored like a webhook secret: never returned by the API, only
`routing_key_set: true`.

Each incident uses the dedup key `celery-manager:<rule id>:<key>`, so the
resolve event closes the PagerDuty incident the trigger opened, and a repeated
trigger for the same open incident does not page twice. `summary`, `source`
(`environment_name`), `component` (the task, queue or schedule), `group` (the
rule kind), the rule's details and a link to the dashboard page are included.
**Send test** triggers an `info`-severity event with the dedup key
`celery-manager:test:-`; resolve it in PagerDuty afterwards.

```json
{"name": "On call", "kind": "pagerduty", "config": {"routing_key": "<32-character integration key>"}}
```

## Security

- **Only admins** can add, change, test or delete rules and channels. Every
  signed-in role can see them and the incidents. See [Roles](roles.md).
- **Secrets are write-only.** A Slack webhook URL is itself a credential:
  whoever has it can post to your channel. The API never returns channel URLs
  in full (only scheme and host, `https://hooks.slack.com/…`) or webhook
  secrets (only whether one is set). Editing a channel without retyping the
  URL keeps the stored one. The audit log records which fields changed, not
  their values.
- **SMTP credentials never touch the database.** They live only in
  `CeleryManagerConfig`.
- **Outbound requests.** Webhook and Slack URLs are entered by an admin and
  requested by the evaluator (and by the API process for **Send test**). Only
  `http://` and `https://` URLs are accepted, redirects are not followed, and
  every request has a time limit. celery-manager does not block private or
  internal addresses: an admin could point a webhook at a service inside your
  network. Of the answer, only the status and the first 200 characters of
  an error response are kept, as the channel's last error (which every
  signed-in role can see). If that matters in your environment,
  restrict where the evaluator and API processes can connect with your
  network policy or an egress proxy (the standard `HTTPS_PROXY` variables
  are honoured).
- Every change and every test message is in the [audit log](dashboard.md#audit-log).

## Production rule kinds (0.3)

All new kinds use the existing incident, quiet-period and channel-delivery
machinery. They are available in the alert-rule editor. A missing observation,
insufficient sample or an excessive scan is **unknown**, not a recovery: existing
incidents remain open and `last_check_error` explains checks that cannot be judged.
Candidate queries are bounded to matching task names and relevant reporting or
baseline windows, with a 20,000-row ceiling. If a required candidate scan exceeds
its ceiling, the check refuses to judge rather than silently reporting a healthy
system; narrow the target/window or use external metrics for larger populations.

| Kind | Target and parameters | Firing and recovery |
|---|---|---|
| `task_failed` | task glob; `window_minutes=60` | One incident per failed task ID in the window. Clears after leaving the window or when the recorded status changes. |
| `consecutive_failures` | task glob; `count=3`, `window_minutes=1440` | Most recent completed runs (ordered by finish time then ID) all failed. A later success recovers. Fewer than `count` completed runs is unknown. |
| `run_stuck` | task glob; `seconds=3600`, `p95_multiplier=3`, `min_runs=10` | A started run exceeds the greater of the configured floor and its task's successful-run p95 over 30 days times the multiplier. With insufficient baseline, uses the explicit floor. Completion recovers; a missing start time is unknown. |
| `worker_lost` | task glob; `seconds=120` | A started run's observed worker is explicitly offline or its heartbeat is stale. A current online heartbeat/completion recovers. A worker never observed is unknown. |
| `beat_stale` | target should be `*`; `seconds=120` | Stored beat heartbeat exceeds the threshold. A fresh tick recovers. No heartbeat or malformed stored heartbeat is unknown. |
| `task_sla` | task glob; `seconds=3600`, `window_minutes=60` | A completed run's recorded runtime exceeds its configured SLA. Clears when that run leaves the lookback window or its runtime is corrected. |
| `duration_regression` | task glob; `multiplier=2`, `baseline_days=30`, `recent_minutes=60`, `min_runs=10` | Recent successful-run p95 exceeds historical successful-run p95 by the multiplier. Baseline ends where the recent window begins: a slow new run cannot inflate its own baseline. Both windows require minimum samples; insufficient samples or zero baseline is unknown. |
| `run_late` | task glob; `seconds=300`, `window_minutes=60` | A scheduled occurrence (stamped by the beat mixin) due in the window started more than `seconds` after it was due, or still has not started that long after. One incident per run. Leaving the window recovers. A finished run with no recorded start is unknown. Firings that never arrived at all are `missed_run`'s job. |
| `daily_digest` | task glob; `hour_utc=8` | One dated incident summarizing the preceding 24-hour UTC reporting window. Details include total runs, status counts and task-by-task status totals. Set `notify_resolved=False` to avoid a resolved notification when the next day's bucket replaces it. |

Daily digest incident keys contain the reporting-window end timestamp. This is a
persisted watermark: evaluator restarts reuse the incident and do not resend a
successfully recorded delivery. Every day is a distinct bucket, including empty
reporting windows once there are retained task observations. It is not an
exactly-once delivery guarantee: a recipient may accept a message before the
sender sees a timeout or before the database records success. Retries can then
produce a duplicate; webhook recipients should deduplicate by rule and incident
key. Tasks with no `finished_at` do not contribute to completed-run digests.

Stuck/lost findings never rewrite execution status or automatically retry work.
A missing heartbeat cannot prove the worker stopped executing; an automatic retry
could duplicate external side effects.


### Deep links in notifications

With `dashboard_url` set, every message links to the page that explains it:
the run itself for `task_failed`, `task_sla`, `run_stuck`, `worker_lost` and
`run_late`; the task overview for `failure_rate`, `consecutive_failures` and
`duration_regression`; the schedule's occurrences for `missed_run`; the queues
or workers page for broker, queue and worker rules. A retry is one click from
the run page; notifications never carry an action that changes state.

## Host probes and independent schedulers

`custom_probe` targets stored probe-name globs with `stale_seconds=120` by default
(1–86400). `scheduler_stale` targets registered scheduler-instance globs with
`seconds=120` (1–86400). Both are available in the alert editor. Degraded evidence
fires, healthy evidence recovers, and unknown/missing/stale probe evidence never
resolves an existing incident. Scheduler state can be degraded by heartbeat age
or an overdue expected command, including its first due occurrence.

The evaluator reads stored observations only; it never invokes a host provider
or scheduler. Configure and collect them through the
[host health APIs](real-world-operations.md#custom-health-probes-and-outbox-diagnostics).
Annotations do not automatically suppress these alerts. Global alert/probe views
remain unavailable in tenant history mode.
