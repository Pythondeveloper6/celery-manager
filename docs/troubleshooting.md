# Troubleshooting and incident runbook

Start every investigation with the read-only preflight. It checks the
database, schema revision, broker, workers and Celery settings, and changes
nothing:

```bash
celery-manager doctor --config myproject.cm:config --check-broker --check-workers
celery-manager doctor --config myproject.cm:config --fix-suggestions   # a checklist with fixes
```

## Symptoms

| Symptom | Likely cause | What to do |
|---|---|---|
| Dashboard shows no runs, or runs stop appearing | The events listener is not running, or workers do not send events | Run `celery-manager events` as its own process. Start workers with `-E` or set `worker_send_task_events = True` and `task_send_sent_event = True`. `doctor --check-workers` reports both. |
| Runs stay **queued** forever | No worker consumes that queue, or the task was never published | Open the queue page: a growing depth with no consumers means a missing worker. Check `task_routes` against the workers' `-Q`. |
| Runs stay **started** after a worker died | The worker was killed before sending a final event | The worker shows as offline on **Workers**. Enable `task_acks_late` and `task_reject_on_worker_lost` so the broker redelivers; on Redis see [Stuck and lost tasks](queues.md#stuck-and-lost-tasks-redis). |
| Login fails with 429 or "try again in" | Login backoff after repeated wrong passwords | Wait for the backoff, or have an admin reset the password on the Users page. Backoff resets after `login_backoff_reset_seconds`. |
| Every request returns 403 "cross-origin request refused" | A proxy rewrites `Host` and the browser's `Origin` no longer matches | Forward the original host (`proxy_set_header Host $http_host`) or add the public origin to `trusted_origins`. |
| 413 "request body too large" | A body larger than `max_request_body_bytes` | Raise the limit if the request is legitimate (large schedule CSV imports). |
| `create_app` refuses to start: secret key | `secret_key` is unset or the placeholder | Set a random 32-byte secret from your secret store. Changing it signs everyone out. |
| `migrate` reports a revision it does not know | The database was upgraded by a newer celery-manager | Upgrade the package, or restore the database backup taken before the upgrade. Downgrades are not supported. |
| Schedules edited in the dashboard do not run | Beat is not using the SQL scheduler, or beat is down | Check the beat liveness card. See [SQL schedules](sql-schedules.md) and [Beat liveness](beat-liveness.md). |
| No alert messages | The `alerts` process is not running, or a channel is misconfigured | Run `celery-manager alerts`. Send a test message from the channel; the last error shows on the channel on the Alerts page. |
| High database load | History grows without retention, or the dashboard polls many tabs | Set retention rules and run `celery-manager prune` on a schedule. Use `scripts/benchmark.py` to size the database. |

## Incident runbook

### A queue is backing up

1. Open **Queues** and the queue page. Note depth, consumer count and the
   oldest message age.
2. No consumers: start or scale workers for that queue. Consumers present but
   slow: check **Workers** for busy pools and the task's duration trend in
   **Insights**.
3. A poison message is failing repeatedly: open **Dead letters**, inspect the
   exception, and fix forward. Retry in bulk only after a fix is deployed; use
   the dry run first.
4. Purging is the last resort. Use the purge preview, which shows the count,
   and type the queue name to confirm. The audit log records who purged.

### Failures spike after a deploy

1. **Dead letters** and **Insights** group failures by task and exception
   type. Record the deploy on the **Operations** page so the timeline shows
   where the change began.
2. Pause the affected schedule if new runs only add noise (**Schedules →
   Pause**, with a reason).
3. Roll back or fix forward. Then select the failed runs, dry-run a bulk retry,
   and retry them.

### Beat stopped scheduling

1. The beat liveness card and the `beat_stale` alert show when the last tick
   was. Missed runs appear on each schedule.
2. Restart beat. Exactly one beat process must run.
3. Missed runs are not replayed automatically. Start the ones that matter with
   **Run now**.

### A worker is stuck or leaking memory

1. **Workers** shows each worker's active tasks and pool. Shrink the pool or
   cancel the stuck task (operator role).
2. Terminating a task kills the pool process. Prefer a soft time limit
   (`task_soft_time_limit`) so the task can clean up.

### A credential leaked

1. Rotate it at the source (SMTP, RabbitMQ, webhook, PagerDuty key) and update
   the channel or config.
2. If `secret_key` leaked, replace it and restart; every session ends.
3. If a dashboard password leaked, reset it; older sessions for that account
   end at once.
4. Review the audit log for actions taken with the credential.

## Collecting information for a bug report

- `celery-manager doctor --config ... --check-broker --check-workers` output.
- celery-manager, Celery, Python and database versions.
- The listener's log. It never logs task arguments or secrets, so it is safe to
  share; review it before posting anyway.
