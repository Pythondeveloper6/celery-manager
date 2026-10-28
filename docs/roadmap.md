# Roadmap

What we plan to build next. Nothing here has shipped yet, and the order may change.
Ideas and use cases are welcome in the
[issue tracker](https://github.com/Pythondeveloper6/celery-manager/issues).

## Know when something goes wrong

**Alerts.** A message to Slack, email or a webhook when a task's failure rate jumps, a
queue backs up, a worker goes offline, or a periodic task misses its run. Rules are set
per task or per queue, with a quiet period so one incident sends one message.

## Teams and access

**Sign in with your app's users.** Use your Django, Flask or FastAPI users, and map them
to celery-manager [roles](roles.md), instead of keeping a separate set of accounts.

## Periodic tasks

**Maintenance windows.** Pause chosen schedules on a recurring window, for example every
Sunday from 02:00 to 04:00, without remembering to resume them.
