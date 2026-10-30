# Production adoption

The 0.2 release combines the production foundations and external scheduler work
identified in the real-project review. Optional adapters remain opt-in. Database
and adapter CI jobs are release gates; local SQL compilation is not evidence of a
successful SQL Server deployment.

## Multiple codebases

Use a distinct database or schema for each codebase/environment. Task UUIDs,
schedule names, worker identities, retention settings and local accounts share a
storage namespace; two unrelated applications must not share that namespace.
Configure the API, workers, producers, listener and beat consistently for each app.

Several Celery apps can be configured in one Python process. Tracking, log/result
policies and progress select the owning task app's runtime using task-local context;
an unconfigured app does not inherit another app's storage. Install a producer hook
for each app. Independent services should still use separate listener deployments
and storage namespaces. Test this topology with the optional adapters and live
services before rollout.

## Safe dispatch

```python
config = CeleryManagerConfig(
    celery_app=celery,
    db_url=database_url,
    secret_key=secret,
    startable_tasks={"imports.preview", "reports.generate"},
    allow_queue_override=False,
    retry_edit_args=False,
)
```

`None` permits registered user tasks; an empty collection disables dispatch through
the dashboard and API. A callable receives `(task_name, principal)` and may make a
per-user decision. Unknown names return 422; policy refusals return 403 and are
audited. Start, retries, bulk retries, failed-run reruns and schedule run-now share
the policy. A per-task Pydantic model in `args_schemas` validates bound task
arguments strictly before publication. Queue overrides require explicit opt-in;
failed-run reruns preserve the original queue. Manager retries and failed-run
reruns preserve the original tenant, correlation ID, operation key and approved
metadata under the current metadata policy, while recording the acting operator
as initiator. Invalid stored context fails before publication; bulk requests
validate every selected row before dispatch.

Single cancellation of an unknown task ID returns 404. An admin may deliberately
use `?force=true`. Bulk and broker-held-message controls keep their existing
semantics.

## Bounded history

Logs default to 256,000 UTF-8 bytes, including a truncation marker between the
beginning and end. Capture retains bounded buffer memory. `log_capture_level`,
`log_capture_loggers` (logger-name prefixes), `log_redactor`, and `log_capture`
control collection. The capture level filters records already emitted by the
host loggers; it does not lower their effective levels. A failed redactor withholds
the raw output. Capture is scoped
to the task's execution context and thread; logs in newly spawned threads are not
attributed to the parent task. `log_view_role` controls API access to captured logs.

Results default to 64,000 bytes (`result_max_bytes`) and can be disabled with
`store_results=False` in both workers and listener. JSON-compatible returns are
encoded as JSON; other values use a representation. Truncation is explicit and
truncated results are display text, not a JSON round-trip format. The decorator's
result takes precedence over the listener's event representation. Existing
[retention rules](retention.md) prune these fields with their task runs.

## External identity

Existing `authenticate_request` integrations continue to work. `ExternalUser` can
also carry a stable `subject`, recorded separately from display names in audits.
Providers in `principal_providers` run first, followed by `authenticate_request`,
then local cookies if local login is enabled. Only a provider returning `None`
falls through; a rejected bearer token cannot fall back to another identity.

```python
from celery_manager.auth.providers import OIDCBearerProvider

provider = OIDCBearerProvider(
    issuer="https://identity.example/tenant/v2.0",
    audience="your-application-id",
    role_map={"Celery.Admin": "admin", "Celery.Read": "viewer"},
)
config.principal_providers = (provider,)
config.local_login_enabled = False
```

Install `celery-manager[oidc]`. Issuer, audience, expiration and signatures are
validated with an explicit asymmetric algorithm allowlist. Roles require an
explicit mapping or `role_mapper(claims)`; unknown permissions grant no access.
The adapter uses cached JWKS and refreshes for a rotated key ID. It validates
bearer tokens; it does not implement an interactive OIDC redirect flow. Use the
host application's existing login flow.

For link-outs into the SPA, opt in to `session_bridge_enabled=True`. A freshly
authenticated `POST /api/auth/bridge` sets a signed dashboard cookie for
`bridge_max_age_seconds` (300 by default). It cannot renew itself. Permission
changes take effect when that short session expires; deployments requiring
immediate revocation should use the host request authenticator on every request.
`session_max_age_seconds` controls local-account sessions.

`trusted_origins` enables credentialed CORS for explicit origins. Cross-origin
cookie writes from other origins remain refused. `frame_ancestors` permits
embedding from explicit origins and removes the conflicting deny-frame header.
These are separate controls: allowing CORS does not allow framing.

## Broker dependencies

For Redis brokers, install `celery-manager[redis]` so Celery's compatible Redis
transport dependencies are installed. RabbitMQ deployments do not need that extra.
The development extra includes Redis because the broker regression tests import
its transport even when no live Redis server is configured.

## Immediate task links and WSGI

```python
from celery_manager import install_publish_hook, task_url

celery_manager.setup(config)
install_publish_hook(celery, initiator=current_person_id)
result = import_task.apply_async(kwargs={"source": "example"})
link = task_url("https://ops.example/celery-manager/", result.id)
```

The optional producer hook stamps the initiator before publication and records a
queued row after successful publication. It is process-local; install it in each
producer process. One installation per Celery app replaces its previous hook.
The hook scopes signals by the current Celery app; multi-app producers should
ensure the publishing app is current. It returns an uninstall callable. Storage
failures do not fail the host's task publication. A link that opens before a row
exists says it is waiting for a record and retries; it does not claim a nonexistent
ID is definitely queued.

Use `live_stream="poll"` with WSGI/a2wsgi. The server omits `/api/stream` and the
SPA polls without repeatedly opening EventSource connections. ASGI deployments
can keep `live_stream="sse"`.

`celery-manager events` enables worker events at startup and every 60 seconds.
Use `--no-enable-events` to opt out. The listener warns after two minutes without
a worker heartbeat. Event enabling does not turn on producer `task-sent` events;
set `task_send_sent_event=True` in producer applications for queue-wait metrics.

## External schedulers

```python
from celery_manager.schedulers import RedBeatBackend
config.scheduler_backend = RedBeatBackend(celery)
```

Install `[redbeat]` or `[django-beat]` for the corresponding backend. Static
`beat_schedule` remains the default, with read-only definitions and the existing
opt-in timing editor. `RedBeatBackend(celery)` and
`DjangoCeleryBeatBackend(celery)` own their definitions in their existing stores.
They preserve entry names and per-entry identity. Missing entries become disabled
history rows; overrides are kept.

Schedule API requests refresh a backend snapshot at most once per
`schedule_sync_interval_seconds` (30 by default). A dashboard refresh discovers
runtime changes without a restart. A backend outage returns 503 and keeps the
stored snapshot; it never disables everything as if the backend were empty.

Use the matching stamping scheduler:

| Backend | Beat scheduler |
|---|---|
| Static | `celery_manager.beat:Scheduler` |
| Static with package timing edits | `celery_manager.beat:DatabaseScheduler` |
| RedBeat | `celery_manager.beat:RedBeatScheduler` |
| Django Celery Beat | `celery_manager.beat:DjangoDatabaseScheduler` |

The existing `DatabaseScheduler` name retains its meaning. During a host migration,
`@track(schedule_from=lambda args, kw: kw.get("schedule_name"),
consume_kwargs=("schedule_name",))` can read a legacy identity kwarg and remove it
before the body runs. Stamped occurrence headers remain authoritative.

Writable backends support:

| Route | Minimum role |
|---|---|
| `POST /api/schedules` | admin |
| `PATCH /api/schedules/{name}` | operator |
| `DELETE /api/schedules/{name}` | admin |
| `POST /api/schedules/preview` | viewer |
| `POST /api/schedules/{name}/skip-next` | operator |
| `POST /api/schedules/{name}/run-now` | operator |

CRUD is audited with before/after definitions. Static-backend CRUD returns 409.
`GET /api/schedules/backend` reports capabilities; `/api/schedules/health` reports
unregistered task references. Schedule run-now is manual and carries no occurrence
stamp, so it does not consume a scheduled skip. Create requires a registered task;
schedule dispatch options reject reserved `cm_` headers.

The backend remains the source of truth. A second package-owned schedule authority,
automatic reconciliation and restoration after Redis data loss are intentionally
not enabled by this release.

## Natural-language schedules

The default parser accepts a limited, deterministic grammar: `every N seconds`,
`every N minutes/hours/days`, `daily at HH:MM`, `weekdays at HH:MM`,
`weekends at HH:MM`, and raw five-field cron. Unsupported or impossible schedules
are rejected. The create form previews ten upcoming runs in the application
timezone before enabling save. Existing schedule edits retain their preview.

For broader language, supply a custom parser or `LLMParser(provider)`, where the
host's provider implements `complete(system, user) -> str`. Rules handle recognized
phrases first; otherwise the AI must return a JSON schedule spec. Its result goes
through the same validation. No SDK, API key or vendor is built into the package.
One expression per entry is supported. Holiday calendars and multi-expression
requests require a future scheduling model. Preview is still needed: syntax
validation cannot prove that a model understood the user's intent.

## Storage isolation and deployment

Set `db_schema="celery_manager"` on databases supporting schemas and provision that
schema before migrating. `engine_options` are passed to SQLAlchemy without mutating
shared model metadata. SQLite uses a separate database file; arbitrary SQLite
attached schemas and table prefixes are not supported. Keep API, worker, listener
and beat storage settings identical.

For deployments with multiple processes:

```sh
celery-manager migrate --db-url "$CELERY_MANAGER_DB_URL" --db-schema celery_manager
```

Then run application processes with `auto_migrate=False`. User-management and prune
commands also accept `--db-schema`. Long-running commands should load the complete
config with `--config myproject.cm:config`.

SQL Server adds a transaction-owned migration application lock and update/range
locks around task-run creation and merging. Server timestamps use UTC defaults.
SQLite, PostgreSQL, MySQL and SQL Server have dedicated CI contracts for migrations,
concurrent writers and pruning. SQL Server additionally needs ODBC Driver 18,
installed outside Python; `[mssql]` installs its Python driver only.

## Operational health

The listener persists worker online/heartbeat/offline observations. Read
`GET /api/health/workers` for last-seen status and `/api/health/runs` for possibly
stuck runs or runs associated with a lost worker. These are advisory findings;
worker loss does not prove execution stopped, so task status is never rewritten.
The initial stuck threshold is configurable through the request's `stuck_seconds`.

For beat liveness, set `app.conf.celery_manager_db_url` and, where needed,
`app.conf.celery_manager_db_schema` on a stamping scheduler. It writes a heartbeat
at most every 30 seconds and bounds sleep accordingly. It does not migrate the
database. `/api/health/beat` reports healthy, stale, or unknown when no heartbeat
has ever been recorded. This assumes one beat owner per storage namespace.

Microsoft Teams channels use Workflows webhooks with Adaptive Cards and the
existing alert incident/dedup/delivery machinery. Webhook URLs remain write-only.
Existing failure-rate, missed-run and worker-offline alerts remain available;
new health findings do not yet introduce separate alert rule kinds.


## Host-owned execution and native operations

The same 0.3 candidate also supports observation-only deployments without a
Celery application. See [real-world operations](real-world-operations.md) for
RunObserver, custom probes/outbox metrics, independent scheduler expectations,
correlation/duplicate visibility, doctor, annotations and action safeguards.
[Tenant history mode](tenancy.md) is a stricter read-only HTTP boundary, separate
from initiator filters and ordinary manager-wide roles. Monitoring storage must
still be provisioned and migrated deliberately when runtime DDL is disabled.
