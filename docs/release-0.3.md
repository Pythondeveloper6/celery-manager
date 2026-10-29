# 0.3.0a1: real-codebase operations

This release continues the work assessed by the 29 October comparison report.
Its useful finding is that SQL schedule authority, exclusion locks, individual
failure handling and operational history matter more than headline coverage
percentages. Those percentages estimated one application's code replacement;
they are not an interoperability or production-readiness test.

The interrupted implementation contained a release plan and partial grammar,
description and timezone changes. Review found that windowed hourly expressions
ignored the requested step and the initial timezone implementation did not
become due correctly. The corrected implementation has regression coverage for
actual beat due checks, UTC projection, daylight-saving transitions and pickle
round trips.

## Scheduling language and descriptions

The deterministic parser accepts weekday lists and ranges (`Mon and Thu at
06:30`, `Mon-Fri at 09:00`), reversed phrasing (`at 06:30 on Mon and Thu`), and
several daily times sharing one minute (`daily at 09:00 and 17:00`). It accepts
`every day` and the existing numbered intervals and raw five-field crons.
It refuses unsupported or ambiguous phrases rather than guessing.

Timezone names can be supplied with a crontab spec or in a phrase such as
`Mon at 06:30 in Europe/Berlin`. They must be IANA names; intervals are elapsed
durations and do not carry a timezone. The preview displays its authoritative
zone. Text requests can pass `timezone` separately. The schedule editor retains
per-entry zones; SQL-backed schedules preserve them on reload and export.
Celery remains responsible for cron's behavior at repeated or missing local
times; this release does not add holiday calendars or a custom DST policy.

An hourly window preserves its starting minute and actual hour step:
`every 2 hours between 09:30 and 17:30` runs at 09:30, 11:30, 13:30, 15:30
and 17:30. Minute windows must fit within one hour: `every 15 minutes between
09:10 and 09:40` runs at 09:10, 09:25 and 09:40. Other window shapes require
separate schedules because one cron cannot express their exact inclusive
boundaries. Several times with different minutes also need separate entries.
Always review the upcoming runs before saving.

Install `celery-manager[schedule-descriptions]` for cron-descriptor descriptions.
Without it, common times/weekday lists have deterministic descriptions and
complex expressions are displayed as cron text. The existing optional host
AI provider remains available and uses the same schedule validation.

## Durable schedules, locks and alerts

- [SQL schedules](sql-schedules.md) describes the optional SQLAlchemy scheduler,
  explicit initialization, SQL authority, dry-run adoption/repair, snapshot
  conflicts and lossless exports. No reconciliation plan deletes extra entries.
- [Singleton execution](singleton.md) documents Redis leases, key selection,
  conflicts, renewal, inspection and conditional administrative release.
  Lease loss cannot stop already-running Python side effects; idempotency
  and downstream constraints remain necessary.
- [Alert rules](alerts.md) adds individual/consecutive failures, stuck/lost,
  stale beat, SLA, runtime regression and a daily digest, including minimum
  samples and unknown-data behavior.
- [Failure inbox](dead-letters.md) adds acknowledge/ignore/reopen and review
  history. These actions never rewrite the task's execution result.
- [Beat liveness](beat-liveness.md) covers multiple owners and an external
  dead-man's-switch URL, so monitoring need not share beat's failure domain.
- [Worker history](workers.md) records deduplicated minute observations with
  bounded retention; gaps remain gaps rather than implied zero activity.
- [Browser sessions](auth-sessions.md) covers shared-database login backoff,
  individual session revocation and upgrade behavior for older cookies.
- [Embedding](embedding.md) covers `/api/v1`, OpenAPI and the status widget.

## Initiator filtering and propagation

`GET /api/tasks?started_by_me=true` matches the authenticated subject when the
host supplies one, otherwise the local username. `initiator=...` permits an
explicit exact filter. Counts, export and matching bulk cancellation share the
same filter. This is a convenience filter, not row-level access control: viewer
access still permits viewing all runs. The dashboard's filter persists in the
URL and saved views.

Dashboard dispatches and operator retries stamp their acting identity in the
message and seed it in history. With `install_publish_hook`, tasks published
inside a worker inherit that request's initiator, including ordinary chains
and group publications. Explicit message identity takes precedence; another
Celery app's current task never supplies a fallback identity. Host-side workflow
roots need the producer hook/initiator callback. The package does not infer
identity for messages published by uninstrumented code.

## Legacy pause CSV import

The CSV schema is exactly `schedule_name,paused_until`; a blank end means an
indefinite pause. Timestamps must include a timezone offset and be in the
future. All schedules must already exist. Names cannot repeat. Imports are
limited to 5,000 rows and 1 MB, validate every row before writing, and commit
all pauses and one audit entry together.

```csv
schedule_name,paused_until
nightly-import,
weekly-summary,2099-01-01T12:00:00+02:00
```

```shell
celery-manager import-pauses pauses.csv --config myproject.cm:config --actor migration-admin
# Review the dry-run output, then apply the same file:
celery-manager import-pauses pauses.csv --config myproject.cm:config --actor migration-admin --apply
```

Administrators can also POST `/api/schedules/pause-import` with
`{"csv":"schedule_name,paused_until\nnightly-import,\n","dry_run":true}`.
Only `dry_run:false` writes. This imports decorator-enforced pauses; tasks must
use `@track` and the stamping scheduler for those pauses to stop their bodies.
It does not change the scheduler backend's enabled flag.

## Drift diagnostics

`GET /api/schedules/health` reports unregistered task references, historical
entries missing from the current backend, and differences between code and
backend definitions. A backend-only definition may be intentional when SQL is
authoritative. Unsupported custom schedules make the comparison unknown.

Add `inspect_consumers=true` to request a bounded live queue-consumer broadcast.
`no_observed_consumer` means no responding worker reported that schedule's queue;
it does not prove there is no worker. A timeout or empty reply is unknown.
These diagnostics never scan arbitrary Redis keys or delete backend data.
Reconciliation plans and audit history provide the reviewable change path.

## Deliberately remaining work

The broad interrupted plan included compatibility projects that require separate
acceptance criteria. This release does not claim them as completed:

- Configurable monitoring table prefixes. Separate database schemas remain the
  supported isolation mechanism; SQLite uses separate files.
- Arbitrary orphan Redis-key discovery/deletion or transactional cross-backend
  reconciliation recovery. No automatic keyspace wipe is introduced.
- Generated SDKs, unrestricted English grammar, multiple cron expressions in
  one entry, holiday calendars or a custom repeated/missing-time policy.
- Domain-specific failure summaries. Host applications still own business
  context and downstream side-effect safety.

Database migrations 0015–0017 add failure review, authentication state and
worker samples. Back up the database and run `celery-manager migrate --check`
after upgrading. The optional SQL scheduler has its own initialization/upgrade
procedure. Local unit tests cannot certify Azure SQL/Redis behavior; the real
storage CI jobs and a rollout in each application's topology remain release
acceptance gates.

## Local verification record — 29 October 2026

| Check | Result |
|---|---|
| Python 3.12 broad runnable suite | 1,395 passed, 27 skipped, 4 deselected |
| Server-independent RabbitMQ cases, run separately | 19 passed |
| New shared-storage login admission/revocation contract | Passed against a file-backed SQLite database |
| Targeted final auth/singleton/SQL/timezone/roles regressions | 349 passed, 2 live Redis cases skipped |
| Widget JavaScript tests | 4 passed |
| Python lint and whitespace | Passed; unrelated pre-existing `.claude` worktrees excluded from local lint |
| Frontend build/lint and committed static bundle comparison | Passed; existing branding fast-refresh and bundle-size warnings remain |
| Strict documentation build | Passed |
| Wheel/sdist build and strict Twine metadata checks | Passed |
| Isolated built-wheel import, migrations, v1 API and widget smoke | Passed |
| CI trigger assertions | Main pushes only; publishing remains downstream of successful main CI |

The broad local sweep excludes socket-backed RabbitMQ-management cases and four
webhook-server cases because this environment cannot bind those servers. Other
skips include opt-in live broker/database gates. The configured real Redis CI
service exercises lease expiry and atomic contention; server storage jobs also
include SQL scheduler persistence and shared-auth admission/revocation. Configuring
a service in CI is not evidence that it has run for this candidate. Browser
visual smoke, Python-version CI, live servers and the host-application trial are
still pending. The checkout could not fetch the report's published remote commit
because GitHub DNS resolution failed, so remote-main ancestry has not been
reconciled here. Work is kept on the local release branch.
