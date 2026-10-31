# Security Policy

celery-manager is alpha software, developed in the open at
https://github.com/Pythondeveloper6/celery-manager.

## Supported versions

| Version | Supported |
| ------- | --------- |
| 0.3.x   | Yes       |
| < 0.3   | No        |

## Reporting a vulnerability

Please report vulnerabilities privately, either by email to
pythondeveloper6@gmail.com or through GitHub's private vulnerability reporting
(the repository's **Security** tab, then **Report a vulnerability**). Do not
open a public issue or pull request for a security problem. Include the
affected version, steps to reproduce, and the impact you observed. You can
expect an acknowledgement as soon as possible; fixes ship in the next release
of the supported line.

## Threat model

### What is protected

- **Control of the Celery cluster**: dispatching, retrying, revoking and
  terminating tasks, purging queues, editing schedules, rate limits and pool
  sizes. These are the most damaging capabilities an attacker could gain.
- **Task history**: arguments, results, tracebacks and captured logs can hold
  personal data or secrets the host application put there.
- **Credentials celery-manager holds**: `secret_key`, `metrics_token`, SMTP
  and RabbitMQ management passwords, alert webhook URLs and PagerDuty routing
  keys, user password hashes.

### Actors and trust boundaries

| Actor | Trusted with |
|---|---|
| Host application code and its configuration | Everything. `CeleryManagerConfig`, `authenticate_request`, `tenant_resolver`, probes and hooks run in-process and are trusted. |
| `admin` | Users, roles, alert channels (including where webhooks are sent), retention and settings. |
| `operator` | Task, queue, worker and schedule actions, within the dispatch policy. |
| `viewer` | Reading history, including arguments unless hidden. |
| Anonymous network client | The login route, `/api/auth/options` and static assets only. |
| Workers and the broker | Event data is treated as untrusted input: sizes are bounded and stored as text. |

### Controls

- **Authentication**: bcrypt passwords (72-byte limit, refused rather than
  truncated), constant-work checks for unknown users, per-account login
  backoff stored in the database, signed `HttpOnly`, `Secure`, `SameSite=lax`
  session cookies with server-side revocation, and an optional OIDC bearer
  provider that requires issuer, audience, expiry and asymmetric algorithms.
  `create_app` refuses the placeholder `secret_key`.
- **Authorization**: every state-changing route declares a minimum role, and
  app start fails if one does not. Roles are re-read from the database on
  every request, so a demotion takes effect immediately. Dispatch policy
  (`startable_tasks`, queue overrides, argument schemas) is checked when the
  action is applied, not only when a form is shown.
- **Tenant isolation**: with `tenant_resolver` set, only routes reviewed for
  tenant scoping answer; every other route is refused (fail closed).
- **CSRF and framing**: state-changing requests whose `Origin` names another
  host are refused; `frame-ancestors 'none'` and `X-Frame-Options: DENY`
  unless `frame_ancestors` is configured.
- **XSS**: React escapes all rendered values; the Content-Security-Policy
  only allows scripts from the dashboard's origin plus its hashed inline theme
  bootstrap. Brand logo URLs are validated (no `javascript:` or SVG data).
- **Injection**: all queries go through SQLAlchemy expressions; no SQL is
  built from strings. Broker and management API paths are URL-quoted.
- **SSRF**: outbound requests go only to URLs configured by the host or an
  admin (alert channels, PagerDuty, heartbeat, RabbitMQ management). HTTP
  redirects are refused so a vetted URL cannot bounce elsewhere.
- **Denial of service**: request bodies are capped (`max_request_body_bytes`),
  list endpoints clamp `limit`, exports cap rows, bulk actions cap the number
  of runs and can require typed confirmation, stored arguments, results,
  logs and observer context are size-limited, and retention bounds history.
- **Secrets in output**: passwords, tokens, webhook URLs and routing keys are
  excluded from `repr`, audit rows, API responses (masked) and logs. Heartbeat
  failures never log the URL. `log_redactor` lets the host scrub captured logs.
- **Audit**: every state-changing action writes an audit row with the actor.
- **Supply chain**: GitHub Actions are pinned to commit SHAs, workflows use
  least-privilege permissions, releases publish from version tags through PyPI
  trusted publishing with attestations, and Dependabot tracks updates.

### Known limits and operator responsibilities

- Admins can point alert webhooks at internal addresses. That is part of the
  admin role; restrict egress at the network level if admins are not fully
  trusted.
- Task arguments and results are visible to every role, as in Flower. Keep
  secrets out of task arguments; `queue_browser_show_args=False` hides
  arguments of messages still waiting on the broker, and `store_results=False`
  and `log_redactor` limit what history keeps.
- Reading a beat `shelve` or RedBeat schedule unpickles it, as Celery itself
  does. Those stores must be writable only by the host.
- `/metrics` uses one shared bearer token, not a user; rotate it like a
  password.
- Run behind TLS. `cookie_secure=False` exists only for local demos.
