---
title: Roles
---

# Roles

Every dashboard account has one of three roles. Each role can do everything
the role before it can.

| Role | For | Can |
|---|---|---|
| `viewer` | People who watch: support, on-call shadowing, managers | See everything. Change nothing. |
| `operator` | People who run things day to day | Everything a viewer can, plus retry (including dead letters), cancel and terminate tasks, start a task, pause, resume and edit schedules, manage maintenance windows, skip single runs, and the worker controls you can undo from the dashboard. |
| `admin` | People who own the deployment | Everything, including purging queues, shutting down workers, changing branding and setting up alerts. |

The server checks the role on every request that changes something. A
request the role doesn't allow gets `403` and does nothing. The dashboard
also hides the buttons a role can't use, but that's only for convenience.
The server check is the real one.

## What each role can do

| Action | Endpoint | Role needed |
|---|---|---|
| Look at anything: task lists and details, schedules, workers, queues, broker health, Insights, the audit log, the live stream | every `GET` | `viewer` |
| See a queue's depth history | `GET /api/queues/{name}/history` | `viewer` |
| Export tasks to CSV or JSON | `GET /api/tasks/export` | `viewer` |
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
  on-call job. Previewing a schedule change is a `POST` only because it
  sends a body; it changes nothing, so viewers may do it.
- **Admin** controls either lose data (a purge deletes messages for good),
  can't be undone from the dashboard (a shut-down worker needs someone to
  start it again), or affect everyone (branding; data retention, which
  decides what is deleted for good; and alerts: a rule or channel decides
  who gets woken up, and a channel holds a webhook URL that is itself a
  credential).

`GET /metrics` is not role-based: it uses its own bearer token
([Metrics](metrics.md)), because Prometheus has no dashboard session.

### What viewers can see

Viewers see task arguments, keyword arguments, captured logs and
tracebacks, and they can export. That's on purpose: a read-only role is
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
change your own password, the browser you did it in stays signed in. Cookies
issued by earlier versions have no fingerprint, so upgrading signs everyone
out once.

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
command line ends up in shell history and in `ps`.

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

The same three roles and the same table above apply; only where the role is
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
