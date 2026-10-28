# Production requirements and roadmap

The real-project adoption review is the requirements baseline. The first combined
release is **0.2.0a1**; it brings production foundations and external scheduling
into one release rather than spreading them across four versions. See
[production adoption](production.md) for the implemented contracts and limitations.

“Candidate” means implemented with regression coverage; optional adapters and live
SQL backends still need their CI/integration gates before a production claim.
“Existing” means the current package already supplies the capability and the
review's older snapshot should not drive a duplicate implementation.

## Adoption requirements

| Review ID | Real-world requirement | Coverage |
|---|---|---|
| P0-1 | Discover runtime schedules outside static Celery config | Candidate: backend protocol, static/RedBeat/Django adapters, request-driven refresh and preserved deletion history |
| P0-2 / F11 | Create, edit and delete schedules with attributable changes | Candidate: API, creation UI, timing editor, deletion confirmation and before/after audits; advanced argument/options updates are API-only |
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
| P2-14 | Remember worker liveness beyond a current ping | Candidate durable last-seen online/offline state; full heartbeat time-series remains open |
| P2-15 | Limit local and bridged session lifetimes | Candidate configurable lifetimes; existing password reset invalidation and live local-role lookup; login backoff and per-session revocation remain open |
| P2-16 | Decorator placement is unambiguous | Candidate corrected docs and import-time rejection of reversed decorator order |
| P2-17 | Import a host's legacy cancellation table safely | Existing Python pause/occurrence controls; documented identity migration; dedicated bulk CSV import remains open |

## Broader product requirements from the review

| Review ID | Capability | Coverage / remaining acceptance criteria |
|---|---|---|
| F1 | Natural-language scheduling | Candidate deterministic grammar, ten-run create preview and optional vendor-neutral AI adapter. Extend to explicit multiple expressions, richer phrases and per-entry timezone behavior before claiming unrestricted language. |
| F2 | Durable schedule authority and reconciliation | Open: explicit ownership mode; dry-run diff; adopt/export; non-destructive repair; conflict handling and crash recovery. Keep the backend authoritative meanwhile. |
| F3 | Skip next / run now | Candidate audited actions and UI. Manual runs must never consume a scheduled override. |
| F4 | Central alerts | Existing rules, incidents, dedup and delivery records; candidate Teams Workflows channel. Add rule kinds for beat stale, individual failures, stuck/lost runs and consecutive failures. |
| F5 | Runtime regression, SLAs and digest | Open: minimum sample size, stable baselines, configurable per-task SLA, persisted digest watermark and delivery dedup. |
| F6 | Stuck/lost runs | Candidate advisory health findings and event-derived worker state. Add task-specific/p95 thresholds and matching alert rules; never assume missing heartbeats prove execution stopped. |
| F7 | Failure inbox | Existing grouped failures and selective reruns. Add acknowledgement/ignore with actor, note and resolution history. |
| F8 | Initiator identity | Candidate publish header, stable stored identity and task detail. Add “my imports” filter and safe propagation through chains/groups/retries. |
| F10 | Stable API and embedded status UI | Open: API version/deprecation contract, generated client, small authenticated task-status widget. |
| F12 | Beat liveness and missed runs | Existing missed-run detection; candidate opt-in beat tick and health API. Add stale-beat rule, detection before a schedule's first observed run, and multiple beat-owner identity. |
| F13 | Drift/orphan health | Candidate unregistered task references. Add no-consumer queues, orphaned backend keys, definition drift and historical changes using F2's ownership model. |
| F14 | Singleton task execution | Open: explicit key semantics, atomic acquisition/release, owner token, renewal/expiry, conflict policy, lock inspection and audited force-release. |

## Additional high-value features

These follow from the same real-world operating conditions, beyond the original
review. They are collected requirements, not promises that unfinished features
are already in the release.

1. **Preflight diagnostics.** A `doctor` command should test configured storage,
   migrations, broker access, task registration, worker consumers, scheduler
   stamping, event visibility and auth configuration, with actionable errors and
   no destructive operations. This would shorten the first production rollout.
2. **Per-schedule timezone and DST policy.** Show which timezone is authoritative,
   how repeated/missing local times behave, and preview upcoming DST transitions.
   Holiday/business calendars should be explicit data, not guessed by a parser.
3. **Publish/execute correlation.** Carry an application request ID and trace
   context through producer, task, retry and workflow lineage so operators can
   move between their application logs and task history.
4. **Environment-aware safeguards.** Clearly label production/staging, support
   read-only mode and require a reason for sensitive operator actions. Task
   policies remain the enforcement layer.
5. **Observability resilience.** Reconnect the listener after broker/DB outages,
   expose collection lag and dropped-write counters, and bound event queues.
   Operators need to know when an empty dashboard means missing observations.
6. **Policy-aware task forms.** Reuse argument schemas to render constrained
   inputs and explain which tasks or argument edits a user can perform before
   they submit a request that will be refused.

## Release verification

- Existing database upgrades and model/migration agreement must pass.
- Policy denials must publish zero messages across every dispatch entry point.
- Large log/result tests must demonstrate byte limits without changing task outcomes.
- Backend contract and stamping tests must pass against supported optional packages.
- Live SQLite/PostgreSQL/MySQL/SQL Server contracts must cover concurrent migrations,
  task-run races, schema isolation, UTC defaults and batched pruning.
- Build, lint, docs, packaged static assets and local install smoke checks must pass.
- A real application trial must verify SSO, deep links, queue routing, separate
  schedules for one task, one-off skips and rollback before retiring Flower or a
  host's existing scheduler layer.

CI runs only after pushes to `main` (including merges). Publishing is downstream
of successful CI on that exact main commit and publishes when that version is absent from PyPI, including retries after CI-fix commits.
Nothing is published by a local merge.
