# Production requirements and roadmap

Adoption requirements for existing Celery deployments are the baseline. The production
foundations shipped in **0.2.0a1**; **0.3.0a1** adds the operational features below
in one release. See [0.3 release scope](release-0.3.md) and
[production adoption](production.md) for contracts and limitations.

“Candidate” means implemented with local regression coverage, not certified in
every deployment. Live PostgreSQL/MySQL/SQL Server, optional scheduler and Redis
contracts, and a production rollout remain acceptance gates. A configured
CI job is not evidence that its live gate has run successfully for this release.

## Adoption requirements

| Review ID | Real-world requirement | Coverage |
|---|---|---|
| P0-1 | Discover runtime schedules outside static Celery config | Candidate: backend protocol, static/RedBeat/Django adapters, request-driven refresh and preserved deletion history |
| P0-2 / F11 | Create, edit and delete schedules with attributable changes | Candidate: API, creation UI, timing editor, deletion confirmation and before/after audits; JSON argument, keyword-argument and delivery-option creation/editing in the dashboard |
| P0-3 | Several schedules of the same task remain independent | Candidate: stamping adapters, identity extractor and consumed identity kwargs |
| P0-4 | Host identity, SSO and stable audit identity | Existing host authenticators; candidate OIDC/JWKS, provider chain, subject, opt-in short session bridge, explicit CORS origins |
| P0-5 | Storage can safely share a production SQL database | Candidate schemas, engine options, UTC defaults and SQL Server locking; live database CI required; table prefixes remain open |
| P0-6 | A dashboard cannot freely dispatch sensitive tasks | Candidate shared dispatch allowlists, strict argument schemas, queue/edit policies, audited denials, explicit unknown-ID cancellation |
| P1-7 | Large or sensitive logs cannot exhaust storage | Candidate bounded head/tail capture, logger/level filters, redaction and role-based log reads |
| P1-8 | Inspect results and control history growth | Candidate bounded results and result opt-out; existing configurable batched retention/prune |
| P1-9 | Monitoring works when workers were not started with `-E` | Candidate periodic event-enabling broadcast and heartbeat warning; worker state reports observations, not a guessed enable flag |
| P1-10 / F9 | Host task links work immediately | Candidate link helper, after-publish seed, initiator header and waiting/recovery page |
| P1-11 | A WSGI deployment cannot dedicate workers to endless streams | Candidate explicit polling mode with no SSE endpoint |
| P1-12 | Embed the dashboard in a host application | Candidate explicit frame ancestors; independent from CORS |
| P2-13 | Scrape Prometheus/OpenTelemetry | Existing; preserve current metrics contract |
| P2-14 | Remember worker liveness beyond a current ping | Candidate minute-bucket heartbeat observations, bounded retention, authenticated range/paging API and worker-card trend/history; missing observations remain unknown |
| P2-15 | Limit local and bridged session lifetimes | Candidate configurable lifetimes, shared DB login backoff and revocable newly issued browser sessions; legacy stateless cookies require expiry, password reset or key rotation |
| P2-16 | Decorator placement is unambiguous | Candidate corrected docs and import-time rejection of reversed decorator order |
| P2-17 | Import a host's legacy cancellation table safely | Candidate validated, all-or-nothing CSV pause import through CLI/admin API; existing Python pause/occurrence controls remain |

## Broader product requirements

| Review ID | Capability | Coverage / remaining acceptance criteria |
|---|---|---|
| F1 | Natural-language scheduling | Candidate deterministic weekday lists/ranges, exact supported time windows, several same-minute daily times, per-entry IANA zones, ten-run create preview and optional AI adapter. Unsupported phrases, multiple expressions, holidays and custom DST policy are deferred. |
| F2 | Durable schedule authority and reconciliation | Candidate optional SQLAlchemy scheduler, explicit initialization, operation-scoped ownership, dry-run diff, adopt/export/repair API and dashboard, snapshot conflicts. Extra entries are never deleted; partial progress requires inspection and a fresh plan, not transactional crash recovery. |
| F3 | Skip next / run now | Candidate audited actions and UI. Manual runs must never consume a scheduled override. |
| F4 | Central alerts | Candidate individual/consecutive failure, stuck/lost, stale-beat, SLA, regression and digest rules in API/dashboard; existing incidents, durable dedup and delivery records. Network timeouts can still duplicate notifications. |
| F5 | Runtime regression, SLAs and digest | Candidate successful-run p95 baseline with minimum samples, per-task/glob runtime SLA, UTC daily per-task outcome totals and persisted incident-key delivery dedup. Insufficient evidence is unknown; candidate scans above 20,000 matching runs in the relevant windows are refused. |
| F6 | Stuck/lost runs | Candidate advisory health findings and matching alerts; stuck rules combine a fixed floor with task-specific successful-run p95, while lost-worker rules use observed offline/stale state. Unknown workers remain unknown; no execution is terminated. |
| F7 | Failure inbox | Candidate acknowledge/ignore/reopen with actor, note and durable review history in API/dashboard; existing grouped failures and selective reruns. Review state never changes the execution result. |
| F8 | Initiator identity | Candidate stored initiator, started-by-me and exact-identity filters across list/count/export/bulk cancel, dashboard saved filters, operator retry identity and instrumented chain/group publication inheritance. Uninstrumented producers cannot supply inferred identity. |
| F10 | Stable API and embedded status UI | Candidate /api/v1 aliases, OpenAPI and dependency-free authenticated task-status widget. Generated language-specific SDKs remain deferred. |
| F12 | Beat liveness and missed runs | Candidate durable per-owner tick observations, aggregate/owner health, stale-beat rule and external dead-man ping independent of DB writes; unknown owners remain unknown. Missed-run inference still starts from observed schedule identity. |
| F13 | Drift/orphan health | Candidate unregistered tasks, historical definitions missing from current backend, definition drift and opt-in observed queue consumers. Empty/failed worker inspection is unknown. Arbitrary orphan Redis-key discovery/deletion is deferred. |
| F14 | Singleton task execution | Candidate track(singleton=...) with explicit keys, Redis owner-token leases, renewal/expiry and skip/retry conflicts; bounded inspection and conditional audited release APIs. No process-local production fallback or fencing guarantee; the dashboard includes bounded inspection and explicit release confirmation. |

## Native operability

These ten capabilities are implemented in the same 0.3.0a1 candidate. Detailed
Python/API signatures, deployment requirements and limitations are in
[real-world operations](real-world-operations.md). “Candidate” remains a local
implementation status, not a completed live-rollout claim.

| Requirement | Candidate scope and boundary |
|---|---|
| 1. Observe non-Celery jobs | RunObserver sync/async contexts and decorators; monitoring-only config accepts no Celery app. Host retains its runner and controls. |
| 2. Tenant visibility/permissions | Trusted sync/async resolver, SQL scoping before paging/count/export, hidden foreign lineage and same-tenant related attempts. Read-only tenant mode denies all writes/global views/events even for admin. |
| 3. Durable background-work diagnostics | Queued, started and terminal observations; host-reported/cancellation interruption and telemetry health. No new delivery engine, hard-kill inference or automatic resume. |
| 4. Custom probes/outbox | Explicit host-collected bounded numeric snapshots, timeout/freshness unknown state, outbox depth/oldest age/exhaustion/latency helper and custom-probe alerts. No host-schema discovery or outbox draining. |
| 5. Scheduler instance expectations | Registered instances, monotonic heartbeat/command observations and first-due checks before first observation; advisory health/UI and alerts. No scheduler replacement or leader election. |
| 6. Request/business correlation | Tenant/correlation/operation IDs plus sanitized allowlisted metadata; trusted publish hook and observation context. Instrumentation must be installed in publishing processes. |
| 7. Duplicate visibility | Operation-key Python lookup and same-tenant operation/correlation related-run UI/API. Matching keys never suppress execution and do not provide exactly-once semantics. |
| 8. Preflight | Read-only doctor CLI/Python and admin Operations view; broker/worker checks are explicit opt-ins. A pass is not end-to-end certification or automatic repair. |
| 9. Deployment/maintenance context | Environment-scoped audited annotations in API/UI. Notes do not automatically pause jobs, suppress alerts or replace schedule maintenance windows. |
| 10. Sensitive action safeguards | Opt-in audited reason and exact environment confirmation on enumerated HTTP actions, after role checks. CLI/Python calls retain their own administrative boundary. |

## Remaining high-value work

- Dedicated calendar transition previews, holiday/business calendars and custom
  missing/repeated-local-time policy; current calendar semantics follow Celery.
- Listener resilience diagnostics beyond current observer health: cross-process
  collection lag, dropped-write counters and bounded event-queue instrumentation.
- Policy-aware task forms generated from argument schemas.
- Configurable monitoring table prefixes and generated language SDKs.
- Arbitrary orphan-key discovery/deletion, automatic transactional cross-backend
  repair and business-specific compensation remain outside this release.

## Release verification

- Existing database upgrades and model/migration agreement must pass.
- Policy denials must publish zero messages across every dispatch entry point.
- Large log/result tests must demonstrate byte limits without changing task outcomes.
- Backend contract and stamping tests must pass against supported optional packages.
- Live SQLite/PostgreSQL/MySQL/SQL Server contracts must cover concurrent migrations,
  task-run races, schema isolation, UTC defaults and batched pruning.
- Build, lint, docs, packaged static assets and local install smoke checks must pass.
- A production trial must verify SSO, deep links, queue routing, separate
  schedules for one task, one-off skips and rollback before retiring Flower or a
  host's existing scheduler layer.

CI runs only after pushes to `main` (including merges). Publishing is downstream
of successful CI on that exact main commit and publishes when that version is absent from PyPI, including retries after CI-fix commits.
Nothing is published by a local merge.
