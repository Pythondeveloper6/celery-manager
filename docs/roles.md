---
title: Roles
---

# Roles

Every dashboard account has one of three roles. Each role can do everything
the role before it can.

| Role | For | Can |
|---|---|---|
| `viewer` | People who watch: support, on-call shadowing, managers | Read operational data, subject to log access policy; manage own browser sessions. No operational writes. |
| `operator` | People who run things day to day | Everything a viewer can, plus retry (including dead letters), cancel and terminate tasks, start a task, pause, resume and edit schedules, manage maintenance windows, skip single runs, review failure inbox entries, and the worker controls you can undo from the dashboard. |
| `admin` | People who own the deployment | Everything, including purging queues, deleting or moving single queued messages, shutting down workers, changing branding and setting up alerts. |

With `tenant_resolver` enabled, [tenant history mode](tenancy.md) adds a stricter
boundary: all operational writes and global views are denied even for admin.
Only scoped history reads are admitted; the role table below describes the
normal manager-wide deployment.

External (`source="external"`) observations remain under host execution control:
Celery retry/cancel/rerun is refused regardless of role. With
`operator_safeguards=True`, the documented sensitive actions also need audited
reason/environment headers after passing role checks; see
[operator preconditions](real-world-operations.md#reasons-and-environment-confirmation-for-sensitive-actions).

The server checks the role on every request that changes something. A
request the role doesn't allow gets `403` and does nothing. The dashboard
also hides the buttons a role can't use, but that's only for convenience.
The server check is the real one. Task dispatch also applies registered-task,
allowlist, argument-schema and queue/edit policy checks; having `operator` is
necessary but does not override those restrictions. `log_view_role` may require
operator or admin for captured logs. See [production policies](production.md#safe-dispatch).

## What each role can do

| Action | Endpoint | Role needed |
|---|---|---|
| Read task lists/details, schedules, workers, queues, broker health, Insights, audit log and live stream | corresponding `GET` routes; exceptions below | `viewer` (logs may require `log_view_role`) |
| See a queue's depth history | `GET /api/queues/{name}/history` | `viewer` |
| Export tasks to CSV or JSON | `GET /api/tasks/export` | `viewer` |
| Create a backend schedule | `POST /api/schedules` | `admin` |
| Update a backend schedule | `PATCH /api/schedules/{name}` | `operator` |
| Delete a backend schedule | `DELETE /api/schedules/{name}` | `admin` |
| Preview a new schedule | `POST /api/schedules/preview` | `viewer` |
| Skip next or run a schedule now | `POST /api/schedules/{name}/skip-next`, `.../run-now` | `operator` |
| Read related operation/correlation attempts | `GET /api/tasks/{task_id}/related` | `viewer`; same tenant as selected run |
| Read stored custom probe/scheduler health | `GET /api/health/probes`, `.../schedulers` | `viewer` |
| Retire an independent scheduler's monitoring expectations | `DELETE /api/health/schedulers/{instance_id}` | `admin` |
| Read deployment/maintenance annotations | `GET /api/operations/annotations` | `viewer`; configured environment |
| Record a deployment/maintenance annotation | `POST /api/operations/annotations` | `admin` |
| Run local/configuration preflight | `GET /api/operations/preflight` | `admin` |
| Read worker heartbeat samples | `GET /api/workers/{worker}/heartbeats` | `viewer` |
| Inspect singleton leases | `GET /api/locks` | `viewer` |
| Conditionally release a singleton lease | `POST /api/locks/release` | `admin`; current lease ID and durable audit required |
| Read failure review history | `GET /api/dead-letters/{task_id}/history` | `viewer` |
| Acknowledge, ignore or reopen a failure | `POST /api/dead-letters/{task_id}/acknowledge`, `.../ignore`, `.../reopen` | `operator` |
| Export scheduler definitions | `GET /api/schedule-reconciliation/export` | `viewer` |
| Generate or apply an adoption/repair plan | `POST /api/schedule-reconciliation/plan`, `.../apply` | `admin` |
| Import legacy schedule pauses (including dry-run) | `POST /api/schedules/pause-import` | `admin` |
| Inspect beat owners and definition/consumer drift | `GET /api/health/beat`, `GET /api/schedules/health` | `viewer` |
| Retire a recorded beat owner | `DELETE /api/health/beat/owner?owner_id=...` | `admin` |
| List own tracked browser sessions | `GET /api/auth/sessions` | any signed-in role; own identity only |
| Revoke own tracked browser session | `POST /api/auth/sessions/{session_id}/revoke` | any signed-in role; own identity only |
| Bridge external authentication | `POST /api/auth/bridge` | `viewer` with fresh external authentication; opt-in |
| Force cancellation of an unknown task ID | `POST /api/tasks/{id}/cancel?force=true` | `admin` |
| Start a task | `POST /api/tasks/start` | `operator` |
| Cancel or terminate a task | `POST /api/tasks/{id}/cancel` | `operator` |
| Retry a task (with or without edited arguments) | `POST /api/tasks/{id}/retry` | `operator` |
| Retry many tasks | `POST /api/tasks/retry-bulk` | `operator` |
| Cancel many tasks | `POST /api/tasks/cancel-bulk` | `operator` |
| Run dead letters again (with or without edited arguments) | `POST /api/dead-letters/rerun` | `operator` |
| Skip or cancel one schedule run | `POST /api/schedules/{name}/occurrences/pause` | `operator` |
| Pause or resume a schedule | `POST /api/schedules/{name}/pause`, `.../resume` | `operator` |
| Preview a schedule change (the next five runs; saves nothing) | `POST /api/schedules/{name}/preview` | `viewer` |
| Change when a schedule runs, or revert it to the code's | `PUT /api/schedules/{name}/schedule`, `DELETE .../schedule` | `operator` |
| List maintenance windows | `GET /api/maintenance-windows` | `viewer` |
| Preview a maintenance window (the next five spans; saves nothing) | `POST /api/maintenance-windows/preview` | `viewer` |
| Create, edit or delete a maintenance window | `POST /api/maintenance-windows`, `PUT`/`DELETE .../{id}` | `operator` |
| Add or remove a worker pool process | `POST /api/workers/{worker}/pool/grow`, `.../shrink` | `operator` |
| Make a worker start or stop consuming a queue | `POST /api/workers/{worker}/queues/add`, `.../cancel` | `operator` |
| Set a task rate limit | `POST /api/tasks/rate-limit` | `operator` |
| Shut down a worker | `POST /api/workers/{worker}/shutdown` | `admin` |
| Purge a queue | `POST /api/queues/{name}/purge` | `admin` |
| See delayed (ETA/countdown) tasks, arguments masked | `GET /api/delayed` | `viewer` |
| See delayed tasks' argument values | `GET /api/delayed?reveal_args=true` | `operator` |
| Cancel delayed tasks | `POST /api/delayed/cancel` | `operator` |
| See stuck and lost (unacknowledged) messages (Redis) | `GET /api/queues/unacked` | `viewer` |
| Put unacknowledged messages back on their queue (Redis) | `POST /api/queues/unacked/restore` | `operator` |
| Browse the messages waiting in a queue | `GET /api/queues/{name}/messages` | `viewer` |
| Delete one waiting message (Redis) | `DELETE /api/queues/{name}/messages/{task_id}` | `admin` |
| Move one waiting message to another queue (Redis) | `POST /api/queues/{name}/messages/{task_id}/move` | `admin` |
| See RabbitMQ nodes, alarms and dead-letter queues, and browse a dead-letter queue | `GET /api/rabbitmq/overview`, `.../dlqs`, `.../dlqs/{name}/messages` | `viewer` |
| Requeue messages from a RabbitMQ dead-letter queue | `POST /api/rabbitmq/dlqs/{name}/requeue` | `admin` |
| Change branding | `PUT /api/settings` | `admin` |
| Change data retention | `PUT /api/settings/retention` | `admin` |
| Preview what retention would delete (deletes nothing) | `POST /api/settings/retention/preview` | `viewer` |
| List users | `GET /api/users` | `admin` |
| Add a user | `POST /api/users` | `admin` |
| Change a user's role | `PATCH /api/users/{username}` | `admin` |
| Reset a user's password | `POST /api/users/{username}/password` | `admin` |
| Delete a user | `DELETE /api/users/{username}` | `admin` |
| Change your own password | `POST /api/me/password` | `viewer` (any signed-in account) |
| See alert rules, channels (URLs masked) and incidents | `GET /api/alerts/rules`, `.../channels`, `.../incidents` | `viewer` |
| Add, edit or delete an alert rule | `POST /api/alerts/rules`, `PATCH`/`DELETE /api/alerts/rules/{id}` | `admin` |
| Add, edit or delete an alert channel | `POST /api/alerts/channels`, `PATCH`/`DELETE /api/alerts/channels/{id}` | `admin` |
| Send a test message to a channel | `POST /api/alerts/channels/{id}/test` | `admin` |

Why the line falls where it does:

- **Operator** controls are the ones you use in an incident and can undo.
  A paused schedule can be resumed, an edited schedule reverted to the
  code's, a pool can grow back, a rate limit can be cleared. Cancel and
  terminate are here too, because stopping a runaway task is a normal
  on-call job, and so is cancelling a delayed task. Putting a stuck Redis
  message back on its queue is here too: it deletes nothing and does what
  kombu itself does after the visibility timeout, and the server refuses it
  for anything a live worker reports holding. Seeing a delayed task's
  argument values also needs operator; viewers see their types only.
  Previewing a schedule change is a `POST` only because it
  sends a body; it changes nothing, so viewers may do it.
- **Admin** controls either lose data (a purge deletes messages for good,
  and so does deleting one waiting message; moving one changes where and
  whether it runs),
  can't be undone from the dashboard (a shut-down worker needs someone to
  start it again), or affect everyone (branding; data retention, which
  decides what is deleted for good; and alerts: a rule or channel decides
  who gets woken up, and a channel holds a webhook URL that is itself a
  credential).

`GET /metrics` is not role-based: it uses its own bearer token
([Metrics](metrics.md)), because Prometheus has no dashboard session.

### What viewers can see

Viewers see task arguments, keyword arguments, captured logs and
tracebacks, and they can export. The same goes for the arguments of
messages still waiting in a queue ([Looking inside a queue](queues.md#looking-inside-a-queue));
`queue_browser_show_args=False` leaves those out of the response for every
role. That's on purpose: a read-only role is
mostly used for debugging, and the list and detail pages already show this
data, so blocking export would only make it slower to get. If your task
arguments hold secrets, don't give that person a dashboard account at all.

## Managing accounts

### From the dashboard

Admins get a **Users** page in the sidebar. It lists every local account (see
[Roles from your own app](#roles-from-your-own-app) for users signed in by your
app) and lets
you add a user, change a role, reset a password (type one, or have a random
one generated and shown once) and delete a user. Every change is recorded in
the [audit log](dashboard.md) as `create_user`, `set_role`, `reset_password`
or `delete_user`. Passwords are never logged. Everyone, whatever their role,
can change their own password from **Change password** in the sidebar; that
needs the current password.

Rules the server enforces, with the reason shown in the dashboard:

- **There is always at least one admin.** The last admin can't be demoted
  or deleted. Make another account an admin first.
- **You can't delete your own account.** Ask another admin. (Deleting
  yourself would end your session mid-request, and another admin can do it
  just as well.)
- **You reset your own password with Change password**, not the admin reset,
  because that asks for the current one.
- Passwords are at least 8 characters and at most 72 bytes. Usernames are
  1-64 characters: letters, digits and `. _ @ + -`, starting with a letter
  or digit (so an email address works). These limits apply to accounts made
  in the dashboard; the CLI and `create_user()` accept shorter passwords as
  before.

**A password change signs the account out everywhere.** The session cookie
carries a fingerprint of the account's password hash, checked on every
request, so a reset or change ends every session issued before it. When you
change your own password, the browser you did it in stays signed in. Very old cookies without a password fingerprint fail that check. Cookies from
before tracked sessions that do have a valid fingerprint remain stateless until
expiry, password reset or signing-key rotation; they cannot be individually
revoked. Newly issued cookies also carry a registered session ID. See
[browser session compatibility](auth-sessions.md#upgrade-compatibility).

Own-session list/revocation, singleton release, beat-owner retirement and CSV
imports are documented API/CLI operations; this release does not add dedicated
dashboard screens for them. Failure review and schedule adoption/repair do have
dashboard controls. See [0.3 release scope](release-0.3.md).

### From the command line

Use the CLI. It works on the same database as the dashboard (`--db-url`):

```bash
# First account: make it an admin.
celery-manager create-user alice --role admin --db-url sqlite:///celery_manager.db

# Read-only by default.
celery-manager create-user bob --db-url sqlite:///celery_manager.db

# Change a role. It applies from that person's next request.
celery-manager set-role bob operator --db-url sqlite:///celery_manager.db

celery-manager list-users --db-url sqlite:///celery_manager.db
```

`create-user` asks for the password twice. To script it, pipe it in with
`--password-stdin`. There's no `--password` flag, because a password on the
command line ends up in shell history and in `ps`. Like the dashboard's user
admin, it requires at least 8 characters and a valid username.

From Python:

```python
from celery_manager.auth.users import create_user, set_user_role

create_user(session, "carol", "a-real-password", role="operator")
set_user_role(session, "carol", "viewer")
```

### Default roles

- **`celery-manager create-user` defaults to `viewer`.** A new account starts
  with the least access. Pass `--role` to give more.
- **`create_user()` in Python defaults to `admin`.** Setup scripts written
  before roles existed call `create_user(session, name, password)` and
  expect a working account. Changing that default would quietly give those
  scripts read-only users.
- **Upgrading makes every existing account `admin`.** Before roles, every
  account could do everything, so nobody loses access on upgrade. Demote
  people afterwards with `set-role`.
- A row added to the `users` table by hand, without a role, gets `viewer`.

A role change, or a deleted account, takes effect on the next request. The
role is read from the database every time, not stored in the session cookie.

## Checking the role from the API

`GET /api/whoami` and `POST /api/auth/login` both return the role:

```json
{"username": "bob", "role": "operator"}
```

A refused request looks like this:

```text
HTTP/1.1 403 Forbidden
{"detail": "Not allowed: this action needs the admin role (you are operator)."}
```

The dashboard shows that message as is.

## Roles from your own app

When celery-manager is mounted in (or next to) a Django, Flask or FastAPI app,
people can sign in with that app's users instead of dashboard accounts (see
[Sign in with your app's users](integrations.md#sign-in-with-your-apps-users)).
Your app then decides each person's role, on every request, through a
`role_for(user)` function that returns `"viewer"`, `"operator"`, `"admin"`, or
`None` for no access:

| Your app | Default mapping | Other options |
|---|---|---|
| Django | superuser → `admin`, staff → `operator`, everyone else → no access | `permission_roles(admin="app.perm", operator=..., viewer=...)` (highest permission held, directly or through a group), or any function of the user (groups, a profile field) |
| Flask | none: you pass `role_for` | any function of your user object |
| FastAPI | none: your function returns the role | anything your token or user record carries |

The same three roles and the table above apply to both `/api` and `/api/v1`; only where the role is
stored changes. Things to know:

- **Changes apply on the next request**, as for local accounts: demote or
  deactivate someone in your app and their next dashboard request has the
  new role, or none.
- **`None` means no access**, shown as a "No access" page naming the account.
  It never falls back to a local account in the same browser.
- **A role that isn't one of the three is refused** with a server error
  (`500`) and a log line, not guessed. A typo like `"Admin"` locks people out
  loudly rather than letting them in quietly.
- **The Users page manages local accounts only**, and is titled "Local
  accounts" while your app signs people in. Users of your app aren't listed:
  their roles live in your app. They have no password here, so **Change
  password** is hidden for them.
- **The audit log names them by the username your app returns.**

Local accounts keep working alongside unless you set
`local_login_enabled=False`. Keeping one local admin is a sensible way back in
if your app's sign-in is ever broken.

## For contributors: adding a route

Any route that changes state must declare a role:

```python
from celery_manager.api.deps import require_admin, require_operator, require_role

@router.post("/{name}/something")
def something(name: str, username: str = Depends(require_operator)):
    ...
```

`require_role(...)` returns the username, like `get_current_user`, so audit
entries still record who did it. `create_app()` refuses to start if any
`POST`, `PUT`, `PATCH` or `DELETE` route doesn't declare a role (login and
logout are the only exceptions). So a forgotten check fails the first test
run, not production. `tests/unit/test_roles.py` also pins the whole table
above, so moving an action to a different role means updating this page too.
