# Observe real application work

This guide covers ten operating capabilities added to the same 0.3.0a1 release:
non-Celery observations, tenant isolation, durable lifecycle diagnostics, custom
probes/outbox measurements, independent scheduler expectations, correlation,
duplicate visibility, preflight, operational annotations and sensitive-action
preconditions. They observe and protect existing execution paths. They do not
install a new executor, delivery engine or business workflow.

## Observe host-owned work

`RunObserver` works without a Celery application. The host keeps its current
async job runner, queue consumer or scheduler and wraps the work it already calls.

```python
from celery_manager import CeleryManagerConfig, RunObserver, current_run

config = CeleryManagerConfig(
    celery_app=None,
    db_url="postgresql+psycopg://monitor:...@db/observations",
    db_schema="job_monitoring",
    auto_migrate=False,
    secret_key="load-a-private-dashboard-key-from-the-environment",
)
observer = RunObserver(config)

@observer.observe("import.customers")
async def import_customers(batch):
    # This function is still invoked and scheduled by the host application.
    await existing_import(batch)

async def handle_request(batch, tenant_id, request_id):
    async with observer.context(
        tenant_id=tenant_id,
        correlation_id=request_id,
        metadata={"integration": "crm", "request_id": request_id},
    ):
        await import_customers(batch)
```

Provision the monitoring schema and deploy its migrations with a separate DDL
credential before running this example. `auto_migrate=False` prevents runtime
migration attempts; it does not create missing tables. Omitting it retains the
package's normal automatic-migration behavior. `RunObserver` construction performs
no database work; persistence initializes lazily on the first operation. A
monitoring-only dashboard uses the same `config` with
`create_app(config)` imported from `celery_manager.api.app`.
It presents observations and supported monitoring/operations views, while Celery
broker, worker and scheduling controls are unavailable.

`observer.observe(name, *, run_id=None, tenant_id=..., context=None,
operation_key=None, correlation_id=..., outcome=None)` returns a handle that is
both a context manager and a decorator. Synchronous and asynchronous functions,
`with` and `async with` are supported. `observer.context(...)` is also synchronous
or asynchronous; nested contexts merge metadata and restore previous context on
exit. `current_run()` returns the active handle in the current thread/async task,
or `None` outside an observed body. It does not create a new execution.

A reusable decorator creates a new run identity on every invocation, including
concurrent calls. It rejects an explicit `run_id`; use a context manager or manual
handle when the host already has an attempt ID:

```python
async with observer.observe(
    "deliver.invoice", run_id=attempt_id, tenant_id=tenant_id,
    operation_key=f"invoice:{invoice_id}", correlation_id=request_id,
    context={"integration": "billing", "resource_id": invoice_id},
) as run:
    receipt = await existing_delivery()
    await run.asuccess({"receipt_id": receipt.id})
```

External IDs are stored with an `external:` prefix; an already prefixed ID is
kept. Unprefixed IDs may contain at most 246 printable characters, prefixed IDs
255; names, tenants, correlation IDs and operation keys allow 1–255 printable
UTF-16 code units when supplied. Basic characters count once; supplementary
characters such as emoji count twice. These portable bounds apply on every
backend and reject oversized identifiers before persistence. IDs identify attempts, not business operations. Reuse
cannot restart or overwrite a terminal observation or reassign its tenant/name/
correlation/operation identity. Invalid instrumentation inputs raise before the
host body begins; fail-open persistence is not input validation.

### Lifecycle and durable background-work diagnostics

Manual handles expose these methods, returning the handle:

| Sync | Async | Observation |
|---|---|---|
| `observer.queued(name, **options)` | `await observer.aqueued(name, **options)` | Create a durable queued observation and return its handle. |
| `run.start()` | `await run.astart()` | Mark execution started; retain the first start timestamp. |
| `run.success(result=None)` | `await run.asuccess(result=None)` | Record success and optional result. |
| `run.fail(exc)` | `await run.afail(exc)` | Record failure with sanitized exception text. |
| `run.skip(reason="Skipped by host")` | `await run.askip(reason=...)` | Record an intentional skip. |
| `run.interrupt(reason=...)` | `await run.ainterrupt(reason=...)` | Record host-reported interrupted execution. |

Context managers enter `started` and exit `succeeded` unless explicitly completed
otherwise. Ordinary exceptions become `failed`; cancellation, `KeyboardInterrupt`
and `SystemExit` become `interrupted`. Exceptions and cancellations propagate to
the host. Async persistence is moved off the event loop; exit makes a shielded
attempt to preserve the terminal observation during cancellation. A hard kill or
machine failure can still leave `queued`/`started` rows: the package cannot infer
whether a business side effect committed. Reconcile such work in the host.

Decorator return values may be classified explicitly:

```python
@observer.observe(
    "sync.inventory",
    outcome=lambda result: "succeeded" if result.ok else "failed",
)
def sync_inventory():
    return existing_sync()
```

The classifier accepts `succeeded`, `failed`, `skipped` or `interrupted`. This
changes the observation, not the host return value. A successful decorator can
store its returned value only when `observer_store_results=True`. For context
managers, call `success(result)` explicitly to supply a result. Terminal
observations are not restarted; a repeated same-terminal write is idempotent.

Every stored row has `source="external"` or `source="celery"`. The dashboard
labels the source and hides Celery action selectors for external work. The server
also refuses retry/cancel/rerun controls for external IDs; a direct request does
not convert host work into a Celery message. Use the host's own retry,
cancellation and recovery controls. This is durable telemetry, not an outbox
implementation, resumable executor or exactly-once delivery mechanism.

### Telemetry failure and privacy

`observer_fail_open=True` is the default: persistence failures are logged by
exception type and counted in `observer.health()`, without retrying or failing the
host execution. Health reports successful/failed telemetry operations and last
success/error timestamps/type. An optional `observer_error_callback(health_dict)`
receives sanitized health, and failures in that callback are contained. Monitor
these signals: missing telemetry is not proof that no work ran.

`RunObserver(config, fail_open=False)` or `observer_fail_open=False` makes
persistence failure raise `ObservationError`; choose this explicitly because it
can prevent a body from starting. While unwinding an existing host exception,
telemetry errors preserve the original error. Neither mode re-executes host work.

Arguments are not captured automatically. Metadata defaults to an allowlist of
`integration`, `request_id`, `trace_id`, `environment`, `deployment_id` and
`resource_id`. Set `observer_metadata_keys` to your own tuple, or `None` only when
you intend to admit all metadata keys. Sensitive key names and recognizable
credential assignments are redacted; this is best-effort sanitation, not a secret
detection guarantee. Unsupported objects are not serialized via arbitrary reprs.
Metadata is bounded in depth/items/string size and by
`observer_context_max_bytes=16384`; an oversized document becomes
`{"_truncated":true}`. Results default off and, when enabled, also use the result
size limit. Do not put secrets in identifiers or free text merely because the
metadata sanitizer exists.

## Tenant visibility and permissions

Enable a trusted `tenant_resolver(request, principal)` to make one dashboard a
**read-only tenant history surface**. It filters SQL before counts/paging and
streamed export; guessed foreign IDs, lineage and failure history do not expose
other tenants. Global operational APIs, metrics, event streams and all writes are
denied even for admin. Host membership validation determines scope; a query
parameter never does. See [tenant isolation](tenancy.md) for configuration, allowed
reads, producer assignment and a separate manager-only administration deployment.

## Request and business correlation

External observations store `tenant_id`, `correlation_id`, `operation_key` and
sanitized `context_json`. Tenant is the visibility boundary; correlation groups a
request/workflow, while an operation key groups attempts of one business action.
Use opaque, nonsecret IDs rather than customer payloads. The default metadata
allowlist provides the integration/request/deployment context alongside them.

Celery producers can attach the same context through the existing publish hook:

```python
from celery_manager import install_publish_hook, setup

sessions = setup(celery_config)
uninstall = install_publish_hook(
    celery_config.celery_app,
    session_factory=sessions,
    context=lambda: {
        "tenant_id": verified_tenant_id(),
        "correlation_id": current_request_id(),
        "operation_key": current_business_operation(),
        "metadata": {"integration": "crm"},
    },
)
```

The context callback is a trusted producer hook, not an authorization check.
It accepts only those four top-level keys. Invalid context is withheld without
changing the publish outcome. Protocol-v2 messages are seeded after successful
publish, so a broker publish failure does not invent a queued row. The hook can
inherit an active observation context or a same-app worker request's `cm_context`
when no callback overrides it. Install it in each producer and in workers that
publish descendants; uninstrumented producers and bypassed publish signals do
not automatically gain metadata. Initiator identity remains a separate field.

Manager-initiated Celery retries, bulk retries and failed-run reruns preserve the
original tenant, correlation ID, operation key and approved metadata, including
when arguments are edited. The current metadata allowlist and size limits are
applied again. The new attempt's initiator identifies the acting operator rather
than inheriting the original initiator. Legacy rows with no context keep their
existing behavior. Invalid stored context is refused before publication; bulk
requests validate every selected row before sending the first message. A broker
failure after validation can still leave a partially published batch.

## Duplicate visibility, not duplicate prevention

`operation_key` reveals related attempts; it never suppresses execution or serves
as a uniqueness lock. Reusing a key with fresh attempt IDs still runs the host
body each time. Tenant, operation-key and correlation matching uses exact stored
identity, including case, accents, Unicode normalization and trailing spaces;
it does not inherit case-insensitive database collation behavior.

```python
attempts = observer.duplicates("invoice:42", tenant_id="tenant-a", limit=100)
# In async code:
attempts = await observer.aduplicates("invoice:42", tenant_id="tenant-a")
```

These lookups return newest external observations only, bounded to 1–500 rows.
`tenant_id=None` means unassigned rows, not all tenants. The explicit Python
observer API is trusted host code; HTTP tenant isolation does not turn arbitrary
host-side calls into an authorization layer.

`GET /api/tasks/{task_id}/related?kind=operation&limit=100` returns same-tenant
attempts sharing the selected run's operation key; `kind=correlation` uses its
correlation ID. Both Celery and external rows can match. Limits are 1–500, and
`has_more` signals truncation. No key means an empty list. Task detail's related
runs panel uses this endpoint. It does not claim business-level success,
exactly-once effects, locking, compensation or automatic deduplication.

## Custom health probes and outbox diagnostics

A host probe returns sanitized aggregate state and finite numeric measurements;
it never gives celery-manager access to the host's database schema implicitly.

```python
from celery_manager.health_probes import (
    ProbeDefinition, ProbeResult, collect_aprobes, outbox_health,
)

async def delivery_outbox():
    values = await host_outbox.read_aggregate_health()
    return outbox_health(
        depth=values.depth,
        oldest_age_seconds=values.oldest_age_seconds,
        retry_exhausted=values.retry_exhausted,
        delivery_latency_seconds=values.delivery_latency_seconds,
        max_depth=1000,
        max_oldest_age_seconds=300,
        max_delivery_latency_seconds=30,
    )

config.health_probes = (ProbeDefinition("billing.outbox", delivery_outbox),)
sessions = setup(config)
# Call periodically from a host-owned observer/monitor loop:
await collect_aprobes(config, sessions)
```

For synchronous host code use `collect_probes(config, sessions)`. Use
`collect_aprobes` when a provider returns a coroutine or Future tied to the host
event loop: provider calls run in worker threads, and returned awaitables resolve
on the calling host loop. With `collect_probes`, returned awaitables resolve on
the runner's own loop; do not use it for resources bound to another loop. Collection
is explicit; the dashboard and alert evaluator read persisted data and never run
providers on an HTTP request. Configure the same expected definitions in the API
and collector processes.

`ProbeResult(state, metrics={})` accepts `healthy`, `degraded` or `unknown`.
Configure up to 32 unique probes with safe names up to 64 characters. Each result
accepts up to 32 safe metric names with finite, non-boolean numeric values whose
absolute value is at most 1e12. Raw exceptions, credentials, free-text explanations
and host payloads are not persisted. Failures become generic `provider_failed`,
`invalid_result` or `provider_timeout` reasons.

`outbox_health(...)` requires all four nonnegative measurements; any `None` makes
it unknown. Retry exhaustion above zero, or reaching any supplied maximum
(`>=`), is degraded. With no exceeded limit and no exhausted retries it is
healthy. This helper does not query, drain, retry or repair the outbox. Define
what latency and oldest age mean in the host, including the empty-outbox case.

Timeout defaults to three seconds. A hung synchronous provider cannot be killed;
one daemon invocation per name stays in flight and subsequent collections report
unknown without stacking invocations. Use I/O timeouts inside the provider.
Persisted observations contain the latest result per name, not a time series.
They become unknown after `probe_freshness_seconds=120`; missing configured probes
are `not_observed`. Collection removes expired latest observations in bounded
batches using `health_observation_retention_days=7`.

View `GET /api/health/probes` or **Operations → Host health**. The `custom_probe`
alert kind matches probe-name globs and uses `stale_seconds=120`: degraded fires,
healthy recovers, missing/stale/failed evidence is unknown and does not resolve
an existing incident. Health reads return 503 if more than 1,000 persisted probe
names would make the snapshot incomplete; alerts treat this as unknown.

## Independent scheduler liveness and expected commands

These records describe host schedulers independently of Celery Beat:

```python
from celery_manager.scheduler_health import (
    ExpectedCommand, register_scheduler, scheduler_heartbeat,
    scheduler_command_observed,
)

with sessions() as session:
    register_scheduler(session, "billing-scheduler", [
        ExpectedCommand("deliver-outbox", "*/5 * * * *", timezone="UTC", grace_seconds=60),
    ])

# From the actual scheduler's liveness loop:
with sessions() as session:
    scheduler_heartbeat(session, "billing-scheduler")

# Report a command observation at the host-defined lifecycle boundary:
with sessions() as session:
    scheduler_command_observed(session, "billing-scheduler", "deliver-outbox")
```

Register expectations at deployment before the first due occurrence. Definitions
use exactly five cron fields, an IANA timezone and grace 0–604800 seconds. Up to
128 commands per instance are accepted. Instance/command IDs must match
`[a-z0-9][a-z0-9_.:-]{0,127}`: 1–128 lowercase ASCII characters, starting
with a letter or digit. IDs are rejected rather than silently normalized, so
the same identity works across database collations and character encodings.
Every helper commits its update. Unchanged definitions
retain their anchors; changing a cron resets its first-observation anchor;
registration removes expectations omitted from the new list. Heartbeat/command
updates are monotonic, so late observations do not move them backwards.

Command due time is the next occurrence strictly after its last reported
observation, or registration time if none exists. It becomes degraded after due
plus grace, even before its first observation. Registering after a missed due
time does not reconstruct earlier expectations. The host decides whether
“observed” means dispatch, start or successful completion; use it consistently.
An observation alone is not proof of successful execution.

`GET /api/health/schedulers` shows heartbeat and per-command state. Heartbeats
older than 120 seconds are degraded; a newly registered owner with no heartbeat
is initially unknown and becomes degraded as its registration ages. Overdue
commands also degrade the instance. Excessive inventory or invalid definitions
become unknown rather than healthy. More than 1,000 scheduler instances returns
503 rather than a partial HTTP inventory; alert evaluation stays unknown.
**Operations → Host health** displays the
stored snapshots. `scheduler_stale` alerts match instance globs, with
`seconds=120` by default; both stale heartbeat and overdue-command state can fire.

`DELETE /api/health/schedulers/{instance_id}` is audited admin retirement of
monitoring expectations, not a command to stop the host scheduler. For Python
use `retire_scheduler(session, instance_id)`; ensure the host has actually retired
it. These APIs do not elect a leader, enforce singleton execution or replace
[Beat-owner monitoring](beat-liveness.md).

## Read-only preflight

```shell
celery-manager doctor --config myproject.monitoring:config
celery-manager doctor --config myproject.monitoring:config --check-broker --check-workers --timeout 5
```

`doctor(config, *, check_broker=False, check_workers=False, timeout=5)` also
returns the report as a Python dictionary. It checks storage reachability and
migration state without initializing or repairing the database, authentication
configuration, tenant/safeguard configuration, and current-process observer
telemetry. With a Celery app it also imports task modules and checks registration
and configured stamping hints. Module import executes the host's normal import
code; keep that code free of operational side effects.

Broker connection and worker-consumer broadcasts require explicit flags. They
report observations, not proof that all expected workers exist. Checks are
bounded by per-check timeouts (0–60 seconds, exclusive of zero); a timed-out
daemon check can finish later and is not forcibly killed. The full sequence can
take more than one timeout. Reports contain sanitized failure types, not DSNs or
exception messages. CLI prints JSON and exits 1 if a check fails, 0 otherwise;
`info`/`unknown` do not by themselves make `ok` false. A pass is not a live
permission, event-collection or end-to-end application certification.

With a Celery app, `doctor` also reviews the settings behind most lost or
duplicated tasks, as `celery_*` checks: `task_acks_late` and
`task_reject_on_worker_lost`, `worker_prefetch_multiplier`, the Redis/SQS
`visibility_timeout` against `task_time_limit` and long ETAs, `result_expires`,
time limits, task events, `task_track_started`,
`broker_connection_retry_on_startup`, and a single-beat reminder. These are
recommendations (`info`), never failures: each open one carries a `fix`.
`--fix-suggestions` prints the whole report as a paste-ready Markdown checklist:

```shell
celery-manager doctor --config myproject.monitoring:config --fix-suggestions > checklist.md
```

Admins can use `GET /api/operations/preflight` or **Operations → Read-only
preflight**. This version uses default local/configuration checks; outbound
broker/worker flags are CLI/Python options. It is denied in tenant mode.

## Deployment and planned-maintenance annotations

The **Operations** page shows environment-scoped deployment and maintenance
history. Admins can record a label, revision and note. The UI starts an annotation
now; API clients may provide an explicit start time.

```http
POST /api/operations/annotations
Content-Type: application/json

{"kind":"maintenance","label":"Database upgrade","revision":"deploy-42","note":"Scheduled window","starts_at":"2026-11-01T01:00:00Z","ends_at":"2026-11-01T01:30:00Z"}
```

`kind` is `deployment` or `maintenance`; labels are 1–255 UTF-16 code units, optional
revision up to 255 UTF-16 code units, note up to 2000 characters. Labels,
revisions and notes preserve Unicode. The configured environment label allows
at most 100 UTF-16 code units. Maintenance requires an end after its start;
any supplied end must be after its start. Omitted start uses now. Environment
comes from `environment_name`, not the request. Creation and its audit entry
commit together; the record includes actor identity.

Viewers can read `GET /api/operations/annotations?limit=50&offset=0` (limit 1–200).
Optional `date_from`/`date_to` filter annotation **start** time, not interval
overlap. Results are newest first with `has_more`; only the configured environment
is returned. These are contextual notes, not automatic alert suppression,
execution pauses or schedule maintenance windows. Existing maintenance-window
controls remain separate. No edit/delete annotation endpoint is introduced.

For a run or incident timestamp, use
`GET /api/operations/context?at=2026-11-01T01:15:00Z`. It returns the latest
deployment at or before that instant and maintenance windows active at the
instant (`starts_at <= at < ends_at`), scoped to the configured environment.
The response contains `at`, `environment`, `deployment` (or `null`),
`maintenance` and `has_more`; at most 200 maintenance windows are returned.
Task detail shows this context at its start time, falling back to its sent time
when it has not started. Tenant-history mode does not expose global annotations.
These records indicate temporal context, not that a deployment caused a failure.

## Reasons and environment confirmation for sensitive actions

```python
config = CeleryManagerConfig(
    ...,
    environment_name="production-eu",
    operator_safeguards=True,
)
```

A nonblank explicit environment is required when safeguards are enabled. It
appears in the dashboard and its public capability response; use a nonsecret
label. After authentication and role checks, protected requests require:

```http
X-CM-Action-Reason: INC-42%20approved%20recovery
X-CM-Confirm-Environment: production-eu
```

Both values are UTF-8 percent-encoded; the decoded reason must be 1–500 printable
characters after trimming. Environment must match exactly. The dashboard prompts
for reason and environment; API clients must send the headers themselves.
Missing/invalid values return 428 without dispatch. Audit-storage failure returns
503 without the action. Accepted and denied intents record environment, acting
identity and a valid reason; acceptance is **intent only**, not proof the later
action succeeded. The action's own audit/result remains authoritative.

Protected actions are task start/retry/cancel (including bulk, dead-letter rerun
and delayed cancel), lock release, queue purge/delete/move/restore, RabbitMQ
requeue, worker shutdown, schedule deletion/reconciliation apply, and scheduler/
Beat-owner retirement. `/api/v1` and mounted paths use the same contract.
`GET /api/auth/options` exposes the enabled action patterns as `sensitive_actions`.
This is an explicit list, not a blanket reason requirement on every write.

Safeguards do not grant a role, override task policy, approve an unsafe retry,
make network operations transactional or turn a tenant app into a manager. Tenant
mode denies these operations regardless of headers. CLI/Python control calls do
not pass through HTTP preconditions; guard those administrative entry points in
the host/deployment separately.

## Upgrade and remaining limits

The monitoring migration head is **0020**: 0018 adds run source, tenant,
correlation, operation key and context; 0019 adds latest probes and independent
scheduler expectations; 0020 adds operation annotations. Deploy migrations with
the proper schema and DDL credential, then use `migrate --check` and preflight.
Back up storage before upgrading. No host application schema is modified by
these APIs; the monitoring namespace must still be explicitly provisioned.

Live SQL/broker/Redis contracts, Python-version compatibility gates and an actual
host rollout remain release acceptance work unless the release evidence records
them as executed. Local mocks and unit tests do not certify an application's
membership logic, outbox thresholds, cancellation safety or scheduler topology.
