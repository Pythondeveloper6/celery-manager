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
