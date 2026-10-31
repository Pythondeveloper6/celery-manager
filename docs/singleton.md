# Singleton task execution

Use `track(singleton=...)` when overlapping executions would compete for the
same resource. This is an explicit execution policy: inability to reach the
lock authority **fails the task before its body starts**. Ordinary tracking
without singleton remains tolerant of monitoring-storage failures.

```python
from celery_manager import CeleryManagerConfig, setup, track
from celery_manager.locks import RedisLockProvider

config = CeleryManagerConfig(
    celery_app=app,
    db_url="postgresql+psycopg://...",
    lock_provider=RedisLockProvider("redis://locks:6379/2", prefix="orders:singleton:"),
)
setup(config)  # also execute this in every worker process

@app.task(bind=True)
@track(singleton=lambda customer_id: f"customer-sync:{customer_id}",
       singleton_ttl=1800, on_conflict="skip")
def sync_customer(self, customer_id):
    ...
```

Apply `track` directly below `app.task`. A key callable receives the task's
user arguments, without bound `self`. `singleton=True` uses the task name;
`singleton="warehouse-export"` uses that exact key. Strings and callable
results share a namespace across task types using the same provider. This
allows two different tasks to guard the same resource deliberately. Keys
must contain 1–512 characters. Never put credentials or private payloads in
keys: authenticated users can inspect active keys and owners.

For Redis brokers, omitting `lock_provider` infers a `RedisLockProvider` from
the broker URL. Other brokers require an explicit provider; there is no
process-local fallback. Separate applications can use independent Redis
databases or namespace prefixes, or deliberately share one authority.
Connection failures are not converted into an unlocked execution.

## Conflicts and task lifecycle

The default `on_conflict="skip"` returns `None` without executing the body.
The task-run row is `skipped`, with the current owner's task ID and task
name in `skip_reason`. Celery itself regards the returned result as success;
the manager preserves its more specific skipped status.

`on_conflict="retry"` calls Celery's normal `task.retry`, with a delay of
`min(singleton_ttl / 3, 60)` seconds. Celery's configured maximum retry count
still applies; exhausting it fails the task. A lock conflict never executes
the body. Prefer skip for periodic jobs when the current execution already
covers the required work; choose retry when each input must eventually run.

Ordinary task exceptions and Celery Retry propagate normally and release
ownership. A crashed worker leaves a lease that expires automatically.
Cleanup failure is logged and the TTL eventually removes the lease; it does
not replace an original task exception or repeat a successful body.

## TTL, renewal and deployment guarantees

`singleton_ttl` is a finite number of at least one second, default 1800.
Each running task has a daemon renewal thread which extends ownership every
`min(TTL / 3, 30)` seconds. Renewal and release compare a random ownership ID
inside Redis Lua scripts. An expired owner's late release or renewal cannot
affect a successor's lock. All keys are hashed under the configured prefix.

Size the TTL above expected process pauses, network interruptions and Redis
latency, with ample margin. Choose Redis socket/connect timeouts below that
margin. The built-in client defaults to five seconds for both. A task that
blocks all threads, suspends its process or loses its Redis connection longer
than the TTL can overlap a successor. **A lease is not a fencing token.**

On renewal failure or ownership loss, the library emits an exception log and
a critical ownership-loss log. It cannot safely terminate an already running
Python body. It checks ownership again when the body finishes and raises
`SingletonLeaseLost`, recording a failed run even when body side effects
already occurred. Do not automatically retry such errors unless the body is
idempotent. Integrations needing strict exclusion despite partitions require
resource-level fencing or database constraints in the host application.

Use a single authoritative Redis deployment configured to avoid eviction of
lease keys. Asynchronous Redis replication failover can lose locks, as can
administrative deletion. This implementation does not claim exclusion across
such failures. For gevent/eventlet workers, verify renewal scheduling with
your chosen monkey-patching and workload; a long non-yielding body can starve
renewal. Keep the TTL conservative. Test the actual deployment topology.

## Inspection and administrative release

`GET /api/locks?limit=100` allows any authenticated role and returns active
lease metadata (`key`, `lease_id`, `task_id`, `task_name`, `acquired_at`,
`ttl_ms`). Inspection is a bounded, best-effort SCAN snapshot, not a complete
transactional inventory. The limit is 1–1000. An unavailable lock authority
returns HTTP 503.

Only admins may call:

```http
POST /api/locks/release
Content-Type: application/json

{"key":"customer-sync:42", "lease_id":"the-current-lease-id-from-inspection"}
```

The ownership ID is required. A stale inspection cannot release a replacement
owner: ownership changes or expiry return HTTP 409. Release intent is audited
before Redis is touched, including actor identity and owner task ID; successful
release or an atomic ownership refusal gets a second audit entry. If audit
storage is unavailable, no administrative release occurs. A Redis connection
failure during release returns 503; inspect again because its outcome can be
uncertain.

Administrative release does **not** stop the running task. Stop/reconcile that
worker and its side effects first. Releasing a live task's lease can permit
an overlapping execution and will eventually report ownership loss in the
old worker.

## Custom providers and testing

`celery_manager.locks.LockProvider` defines `acquire`, `renew`, `release`,
`inspect`, and bounded `list`. `acquire` returns `(acquired, Lease)`; on a
conflict the lease identifies the current owner. Renew/release must atomically
compare `lease_id` and must never modify a successor. Providers must be safe
for multiple worker processes and for concurrent renewal threads. Acquire
must establish an expiring lease atomically. An in-memory implementation is
suitable only as a test double, not a production backend.

The unit suite exercises eager Celery tasks, concurrent contention, renewal,
fail-closed outages, per-app authority isolation, role enforcement and audit
history with a synchronized test-only provider. Additional contract tests
start an already-installed local `redis-server` and exercise real Lua/expiry;
they skip when the binary or socket permission is unavailable. A skipped
contract does not establish live Redis support for a deployment.

A callable singleton key receives the same user arguments as the task body: names declared in `consume_kwargs` have already been removed. Inspection matches only the configured namespace and its hashed keys, including when another namespace shares its prefix.
