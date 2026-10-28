---
title: RabbitMQ
---

# RabbitMQ

On RabbitMQ, celery-manager can read the
[management API](https://www.rabbitmq.com/docs/management) for what a plain
AMQP connection can't tell it:

- per queue: consumers, unacknowledged messages, publish and deliver rates,
  queue type (classic, quorum or stream), maximum priority, dead-letter
  exchange and routing key, and how long it has been idle;
- per node: memory and disk use against their limits, memory and disk
  **alarms**, file descriptors and sockets;
- **dead-letter queues**: what RabbitMQ dead-lettered, why, and a way to send
  it back where it came from.

All of it is off until you configure the management API, and stays off when
the broker isn't RabbitMQ.

## Setup

### Enable the management plugin

```bash
rabbitmq-plugins enable rabbitmq_management
```

The `rabbitmq:3-management` Docker images have it already. It listens on port
15672.

### The management user

Give celery-manager its own user. Reading needs only the `monitoring` tag:

```bash
rabbitmqctl add_user celery-manager 'a-long-random-password'
rabbitmqctl set_user_tags celery-manager monitoring
rabbitmqctl set_permissions -p / celery-manager '' '' ''
```

That covers the Queues page panel, metrics, the `rabbitmq_alarm` alert and the
list of dead-letter queues. Two things need more:

| To | The management user also needs |
|---|---|
| Browse a dead-letter queue's messages | `read` permission on that queue in the vhost. RabbitMQ can only show a message by handing it out and taking it back, which is a read. |
| Requeue dead-lettered messages | Nothing more on the management user: requeue runs over AMQP with **the broker URL's user** (your Celery user), which needs `read` on the dead-letter queue and `write` on the exchanges the messages came from. It already has both if your workers use it. |

For example, to let the management user browse queues ending in `.dlq`
without touching anything else:

```bash
rabbitmqctl set_permissions -p / celery-manager '' '' '^.*\.dlq$'
```

### Configuration

```python
from celery_manager import CeleryManagerConfig

config = CeleryManagerConfig(
    celery_app=app,
    rabbitmq_management_url="http://rabbit:15672",
    rabbitmq_management_username="celery-manager",
    rabbitmq_management_password=os.environ["RABBITMQ_MANAGEMENT_PASSWORD"],
)
```

| Option | Default | |
|---|---|---|
| `rabbitmq_management_url` | `None` | The management API. `https://` works; redirects are never followed, because following one would send the credentials somewhere else. Put the address it redirects to here instead. |
| `rabbitmq_management_username` / `_password` | `None` | Sent as a Basic auth header. The password is never stored in the database, returned by the API, logged, or shown in the config's `repr()`. Don't put credentials in the URL. |
| `rabbitmq_management_verify_tls` | `True` | Turn off only for a test broker with a self-signed certificate. |
| `rabbitmq_management_timeout_seconds` | `3.0` | Time limit for one operation (all its HTTP calls together). |
| `rabbitmq_dlq_patterns` | `["*.dlq", "*.dead", "dlq.*"]` | Queue names that count as dead-letter queues even if nothing dead-letters to them. |

The vhost comes from the broker URL (`amqp://user:pass@rabbit:5672/prod`
uses the vhost `prod`; `//` at the end is the default vhost `/`).

## On the Queues page

A **RabbitMQ** panel shows the version, each node's memory against its
high-watermark, free disk against its limit, file descriptors and sockets. A
red banner names any node with a memory or disk alarm: while it lasts,
RabbitMQ blocks every publisher, so new tasks can't be sent.

Under each queue's name: its type, unacknowledged messages, publish (in) and
deliver (out) rates, priority range, where it dead-letters to, and how long
it has been idle. Rates are what the management API samples (every 5 seconds
by default) and show `—` until it has a sample.

If the management API can't be reached, the panel says why (`401` means check
the credentials) and the rest of the page works as before.

Without a management URL on a RabbitMQ broker, the panel explains how to set
it up. On other brokers it isn't shown.

## Dead-letter queues

RabbitMQ *dead-letters* a message when a consumer rejects it without requeue,
its TTL expires, the queue is over its length limit, or a quorum queue's
delivery limit is hit. It goes to the queue's dead-letter exchange
(`x-dead-letter-exchange`, as a queue argument or a policy), and from there
to whatever queue is bound to it.

**Which queues count.** A queue is a dead-letter queue if:

- some queue's dead-letter exchange and routing key route to it (read from
  queue arguments and policies, through the exchange's bindings: direct,
  topic, fanout and the default exchange); or
- its name matches one of `rabbitmq_dlq_patterns`.

The page also warns when a queue dead-letters to an exchange with nothing
bound, because RabbitMQ then drops those messages.

Open **Queues → Dead-letter queues** (`#/rabbitmq/dead-letters`).

### Browsing

The first 20 messages of the selected queue, with the task name and id, and
each `x-death` entry: the reason (`rejected`, `expired`, `maxlen`,
`delivery_limit`), the queue and routing key it died in, how many times, and
when.

Browsing is **approximate and has side effects**. RabbitMQ has no way to look
at a message without handing it out, so celery-manager asks the management
API to take each one and put it straight back (`ack_requeue_true`). The
messages come back marked *redelivered* and may change order, and a consumer
on the dead-letter queue could take one meanwhile. For that reason the page
reads only when you open a queue or press Refresh; it doesn't poll.

**Arguments are hidden by default**, for every role, and shown when you tick
**Show arguments** (`reveal_args=true`). This is the same policy as task runs,
where every role can see arguments (see [Roles](roles.md#what-viewers-can-see));
it only keeps them off the screen until someone asks. Celery's own
`argsrepr`/`kwargsrepr` headers are shown when present, so an app that already
masks arguments in them stays masked.

### Requeue

Admins can requeue the selected messages, or the first 100. Each message goes
back to the **exchange and routing key of its newest `x-death` entry**: where
it was published before it died.

Nothing is lost on failure. Requeue runs over AMQP and, for each message:

1. takes it from the dead-letter queue **without acknowledging it**;
2. publishes a copy to the original exchange and routing key, *mandatory* and
   with **publisher confirms**;
3. waits for RabbitMQ to confirm the copy, and checks it wasn't returned as
   unroutable (for example because the original queue is gone);
4. only then acknowledges the original, removing it from the dead-letter queue.

The first failure stops the run. The message being moved is put back, later
ones are never taken, and the result says how many moved and why it
stopped. The worst case is a duplicate: if the connection drops after the
confirm but before the acknowledgement, the message is both requeued and
still in the dead-letter queue.

Every requeue is in the [audit log](rest-api.md#audit-log) as
`requeue_dead_letters`, with the task ids moved.

**The copy keeps its headers, `x-death` included**, and gets an
`x-celery-manager-requeues` header counting how often celery-manager has
requeued it. Keeping `x-death` keeps the message's history visible if it dies
again. The counter matters on RabbitMQ 4, which ignores `x-death` on messages
a client publishes and starts its count again.

**Loop protection.** A message that has died more than 3 times, by its
`x-death` counts or by that requeue counter, is refused and stays in the
dead-letter queue. Something keeps rejecting it, and sending it round again
just repeats that. Tick **Also send messages that have died more than 3
times** (`force`) to send it anyway. A message without an `x-death` header is
also refused, since where it came from is unknown.

## Metrics and alerts

With management configured, `/metrics` adds
`celery_manager_rabbitmq_queue_unacked`, `_queue_consumers`,
`_queue_publish_rate`, `_queue_deliver_rate` per queue and
`celery_manager_rabbitmq_mem_alarm` / `_disk_alarm` per node
(see [Metrics](metrics.md)).

The `rabbitmq_alarm` [alert kind](alerts.md#rule-kinds) fires while a node
matching its target reports a memory or disk alarm.

## REST API

`GET /api/rabbitmq/overview`, `GET /api/rabbitmq/dlqs`,
`GET /api/rabbitmq/dlqs/{name}/messages` and
`POST /api/rabbitmq/dlqs/{name}/requeue`; see the
[REST API reference](rest-api.md#rabbitmq).
