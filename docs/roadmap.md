# Roadmap

What we plan to build next. Nothing here has shipped yet, and the order may change.
Ideas and use cases are welcome in the
[issue tracker](https://github.com/Pythondeveloper6/celery-manager/issues).

Everything on the previous roadmap (alerts, missed-run detection, metrics, sign-in with
your app's users, user management, schedule editing, maintenance windows, the
dead-letter view and data retention) shipped in 0.1.0a5. See the
[changelog](changelog.md).

## Know your broker

These use Redis or RabbitMQ directly, beyond the queue depth celery-manager reads today.

**Queue wait time.** How long tasks wait in the queue before a worker starts them, as
p50/p95 per queue, a metric, and a "tasks are waiting too long" alert.

**Queue depth history.** Depth over time for each queue, as small charts, and an alert
when a queue keeps growing for a number of minutes.

**Delayed tasks.** Tasks held by workers with an ETA or countdown, in one list, with
cancel.

**Stuck and lost tasks (Redis).** Tasks a worker took but never acknowledged, for
example after a crash, with how long they have been held and a way to put them back
on the queue.

## Look inside a queue

**Browse waiting messages.** Task name, id, arguments (masked) and age of the messages
waiting in a queue, without taking them off it. Exact on Redis; approximate on
RabbitMQ, where reading a message changes its position.

**Delete or move a single message (Redis).** Remove one waiting message, or move it to
another queue, with an audit entry.

## RabbitMQ

**Management integration.** Consumers and unacknowledged messages per queue, publish
and deliver rates, queue type (classic or quorum) and node alarms, from the RabbitMQ
management API.

**Dead-letter queues.** See what RabbitMQ dead-lettered and send it back to the queue
it came from.
