# Dashboard walkthrough

The dashboard is a single-page React app served by the same FastAPI app as
the REST API — once built, its static files live at
`src/celery_manager/dashboard/static/` and are mounted at `/`. It uses
`HashRouter` rather than browser history routing (the server has no
catch-all fallback route, so a real GET for e.g. `/tasks/abc` would 404
before React ever loads — routing on the URL fragment keeps every deep
link resolving to the same `/` request the server actually serves).

Every screen calls the [REST API](rest-api.md) with `credentials:
"include"` so the session cookie set at login is sent automatically; a
`401` from any call routes the user back to the login screen (via the
top-level `whoami()` check in `App.tsx`).

## Login

The entry screen (shown whenever `GET /api/whoami` comes back unauthenticated,
or the initial connectivity check to the server fails). A simple
username/password form:

- Inputs are locked (`disabled`) while a login request is in flight, so a
  slow network can't produce a double-submit.
- A failed login shows an inline error (`role="alert"`) without leaking
  whether the username or the password was wrong — both `401` cases are a
  single generic message.
- A network-level failure (server unreachable, DNS/CORS issue) is shown as
  a distinct "connection problem" screen with a **Retry** button, rather
  than being folded into the "invalid credentials" message a real login
  attempt would show — the two are different problems.

On success, the app switches straight to the task list; there's no
separate "remember me" control — the session cookie lasts 7 days by
default (see [Configuration](configuration.md#secret_key-in-production)).

## Navigation

A thin persistent nav bar sits above every authenticated screen: the
brand mark/wordmark on the left, and **Tasks** / **Audit Log** links on
the right (`NavLink`, so the active screen is highlighted). Each screen
underneath still owns its own page header — the nav bar deliberately
doesn't duplicate per-screen titles or actions.

There's currently no nav entry for schedule occurrences — that screen is
reached via a schedule name in the URL (`/schedules/:name`) and there's no
"list all schedules" endpoint or screen yet to link to from the nav (a
known gap, not an oversight).

## Task list

The default screen after login (`/`). Shows every task run celery-manager
knows about, newest first.

- **Status filter bar** — buttons for `all` plus every status
  (`queued`/`started`/`succeeded`/`failed`/`retried`/`skipped`/`revoked`);
  the active filter is visually pressed (`aria-pressed`) and re-fetches
  `GET /api/tasks?status=...`.
- **Live updates** — the screen opens an SSE connection
  (`GET /api/stream`) on mount and silently refetches the current filter's
  task list whenever *any* task event arrives, rather than trying to patch
  individual rows from the event payload. The connection is kept open
  across filter switches (not torn down and reopened) so switching filters
  repeatedly doesn't reconnect the stream. New data dims the existing
  table briefly rather than showing a full loading state, so rows never
  flicker away while fresher data is in flight — only the very first load
  shows the full-page "Loading tasks…" state.
- **Bulk retry** — only available while the `failed` filter is active.
  Each row gets a checkbox; a bulk action bar appears once at least one
  row is selected, showing the selected count and a **Retry N selected**
  button that calls `POST /api/tasks/retry-bulk`. The selection is cleared
  whenever the filter changes, and is also pruned (not wholesale cleared)
  against the live task list on every refresh — so a task that moves out
  of "failed" via a background SSE event can never stay silently selected
  for a bulk action the user no longer sees it queued for.
- Each row links to that task's detail page; task name and task id are
  both shown (the id in a distinct, monospace style).

## Task detail

`/tasks/:taskId`. Shows one task run in full:

- Status badge, and a **Retry** button shown only when `status ===
  "failed"` — calls `POST /api/tasks/{task_id}/retry` and, on success,
  shows a link to the newly created run (`new_task_id`) rather than
  navigating away, so the user can compare old and new runs side by side
  if they want.
- Args and kwargs, pretty-printed as JSON (the API returns them as raw
  JSON strings; the page re-parses and re-serializes with indentation for
  display, falling back to the raw string if parsing fails).
- A **"Why it failed"** panel — exception message plus full traceback —
  shown only when the run has a recorded exception.
- A **log output** panel showing everything captured via `@track`'s log
  capture during the run (see [The decorator](decorator.md)), or an empty
  state if the task wasn't decorated or produced no output.

## Schedule occurrences (future-schedule view)

`/schedules/:name`. Lists the schedule's next 10 computed occurrences
(`GET /api/schedules/{name}/occurrences`) and lets a user pause one:

- Each row is in one of three states, computed client-side from the API
  response: **pending** (no override yet), **paused** (an override with
  action `skip` or `cancel` exists — both read as "a human has already
  decided this occurrence's fate"), or **resolved** (`consumed: true`,
  meaning the occurrence's scheduled time has actually passed — in
  practice every row this screen shows is still in the future, so this
  state only appears defensively, if the API were to ever return a
  consumed row).
- **Skip this run** / **Cancel this run** buttons on each non-resolved
  row call `POST /api/schedules/{name}/occurrences/pause` with that row's
  `target_time` and the chosen action. A button already matching the
  row's current action shows as pressed/labeled accordingly (e.g. "Skipped")
  rather than being hidden, so re-clicking the same action or switching to
  the other one both work as an update in place — matching the pause
  endpoint's own update-in-place idempotency (see
  [REST API: occurrence-matching nuances](rest-api.md#occurrence-matching-nuances-worth-knowing)).
  There is no "reschedule" control in the UI, and the API no longer accepts
  that action value either (it returns `422`) — the data model still has the
  column, but no reschedule behavior is implemented.
- A per-row busy/error state means one occurrence's pause request failing
  doesn't block or hide the others; a page-level success notice
  auto-dismisses after 6 seconds.

## Audit log

`/audit`. A read-only table of every manually-triggered control action —
currently just task retries (single and bulk) — with **When**, **User**,
**Action**, and **Target** (`{target_type}:{target_id}`, e.g. `task:abc123`)
columns, newest first, backed by `GET /api/audit`. There's no filtering or
pagination control in the UI beyond the API's default 50-row limit
(`limit` is a raw query param on `GET /api/audit` but the screen doesn't
currently expose a control for it). An empty state ("No manual actions
recorded yet.") is shown until the first control action is taken through
the dashboard.

This is the audit trail that answers "who did what, to what, and when" for
anything the dashboard itself performed on a user's behalf — it does not
record automatic system behavior (task status transitions recorded by the
events listener, or automatic skip/cancel outcomes recorded by `@track`);
those show up in the task list and detail screens instead, not here.
