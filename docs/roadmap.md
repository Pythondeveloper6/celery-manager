# Roadmap

What we plan to build next. Nothing here has shipped yet, and the order may change.
Ideas and use cases are welcome in the
[issue tracker](https://github.com/Pythondeveloper6/celery-manager/issues).

## Know when something goes wrong

**Alerts.** A message to Slack, email or a webhook when a task's failure rate jumps, a
queue backs up, a worker goes offline, or a periodic task misses its run. Rules are set
per task or per queue, with a quiet period so one incident sends one message.

**Missed-run detection.** Beat now stamps every scheduled run with the time it was due
(see [the decorator](decorator.md)). The schedule page will show a run that was due but
never arrived, which today goes unnoticed.

**Prometheus and OpenTelemetry export.** A `/metrics` endpoint with run counts,
durations, failure rates and queue depth, for teams that already watch Grafana.

## Teams and access

**Sign in with your app's users.** Use your Django, Flask or FastAPI users, and map them
to celery-manager [roles](roles.md), instead of keeping a separate set of accounts.

**User management in the dashboard.** Admins can add users, change roles and reset
passwords from the UI. Today this is done with the `celery-manager` command.

## Periodic tasks

**Edit schedules from the dashboard.** Change a cron expression or an interval and see
the next five runs before saving. This needs a database-backed beat scheduler, such as
django-celery-beat.

**Maintenance windows.** Pause chosen schedules on a recurring window, for example every
Sunday from 02:00 to 04:00, without remembering to resume them.

## Failed work

**Dead-letter view.** Runs that used up all their retries, in one place, with "fix the
arguments and run again" for one run or many.

## Running it for a long time

**Data retention.** Delete runs and their logs after a number of days you choose, per
task, so the database does not grow forever.
