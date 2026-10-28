---
title: Roles
---

# Roles

Every dashboard account has one of three roles. Each role can do everything
the role before it can.

| Role | For | Can |
|---|---|---|
| `viewer` | People who watch: support, on-call shadowing, managers | See everything. Change nothing. |
| `operator` | People who run things day to day | Everything a viewer can, plus retry (including dead letters), cancel and terminate tasks, start a task, pause, resume and edit schedules, skip single runs, and the worker controls you can undo from the dashboard. |
| `admin` | People who own the deployment | Everything, including purging queues, shutting down workers and changing branding. |

The server checks the role on every request that changes something. A
request the role doesn't allow gets `403` and does nothing. The dashboard
also hides the buttons a role can't use, but that's only for convenience.
The server check is the real one.

## What each role can do

| Action | Endpoint | Role needed |
|---|---|---|
| Look at anything: task lists and details, schedules, workers, queues, Insights, the audit log, the live stream | every `GET` | `viewer` |
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
| Add or remove a worker pool process | `POST /api/workers/{worker}/pool/grow`, `.../shrink` | `operator` |
| Make a worker start or stop consuming a queue | `POST /api/workers/{worker}/queues/add`, `.../cancel` | `operator` |
| Set a task rate limit | `POST /api/tasks/rate-limit` | `operator` |
| Shut down a worker | `POST /api/workers/{worker}/shutdown` | `admin` |
| Purge a queue | `POST /api/queues/{name}/purge` | `admin` |
| Change branding | `PUT /api/settings` | `admin` |
| List users | `GET /api/users` | `admin` |
| Add a user | `POST /api/users` | `admin` |
| Change a user's role | `PATCH /api/users/{username}` | `admin` |
| Reset a user's password | `POST /api/users/{username}/password` | `admin` |
| Delete a user | `DELETE /api/users/{username}` | `admin` |
| Change your own password | `POST /api/me/password` | `viewer` (any signed-in account) |

Why the line falls where it does:

- **Operator** controls are the ones you use in an incident and can undo.
  A paused schedule can be resumed, an edited schedule reverted to the
  code's, a pool can grow back, a rate limit can be cleared. Cancel and
  terminate are here too, because stopping a runaway task is a normal
  on-call job. Previewing a schedule change is a `POST` only because it
  sends a body; it changes nothing, so viewers may do it.
- **Admin** controls either lose data (a purge deletes messages for good),
  can't be undone from the dashboard (a shut-down worker needs someone to
  start it again), or affect everyone (branding).

### What viewers can see

Viewers see task arguments, keyword arguments, captured logs and
tracebacks, and they can export. That's on purpose: a read-only role is
mostly used for debugging, and the list and detail pages already show this
data, so blocking export would only make it slower to get. If your task
arguments hold secrets, don't give that person a dashboard account at all.

## Managing accounts

### From the dashboard

Admins get a **Users** page in the sidebar. It lists every account and lets
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

## Embedding in your own app

celery-manager has no hook for plugging in your host app's login. When you
mount it inside Django, Flask or FastAPI, it still uses its own accounts and
its own `cm_session` cookie (see [Integrating with your app](integrations.md)).
So roles work the same way when embedded: manage them from the Users page or the CLI.

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
