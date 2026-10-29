# Versioned API and embedded task status

New integrations can use `/api/v1` (for example `/celery/api/v1/tasks` when
mounted under `/celery`). The existing `/api` paths remain compatible aliases.
Both paths execute the same endpoint, authentication, dispatch policy, roles,
CSRF checks and scheduler refresh dependencies. Versioned paths are included in
`/openapi.json`; their operation IDs are distinct from the legacy aliases.
The dashboard continues to use the existing paths.

Within v1, integrations should tolerate additional response fields and new
status values. Breaking changes to documented request/response meanings require
a new API namespace and a migration notice in the changelog; introducing v1
does not remove the legacy namespace. This is an alpha release: perform an
integration trial against your application's authentication and mounting setup.
A generated language-specific SDK is not shipped. Generate one from your
running deployment's OpenAPI document if your project needs it.

## Small status widget

The package ships `task-status.js`, a dependency-free JavaScript module. It
renders the task name and current state into an element you supply. It uses
`textContent`, so task names are never interpreted as HTML. It does not expose
arguments, logs or results.

```html
<div id="import-status"></div>
<script type="module">
  import { mountTaskStatus } from "/celery/task-status.js";
  const destroy = mountTaskStatus(document.querySelector("#import-status"), {
    taskId: "your-celery-task-id",
    baseUrl: new URL("/celery/", window.location.origin).href,
    interval: 5000,
  });
  // Call destroy() when your host application's component unmounts.
</script>
```

The URL is the dashboard mount, not its `/api` path. Polling uses `/api/v1/tasks`
and browser credentials. The interval is clamped to 1–60 seconds, each request
has a ten-second timeout, and requests never overlap. A missing task record
keeps polling. Completed, failed, skipped and revoked tasks stop polling; a
retried task continues. A 401 or 403 displays a sign-in message and stops;
remount after signing in. Other failures display a retry message. `destroy()`
cancels timers and the active request without altering the host element later.

Same-origin embedding is the simplest deployment. For another origin, configure
`trusted_origins` explicitly and verify the host's cookie/SameSite policy.
The widget neither stores tokens nor puts credentials in a URL. Applications
using bearer authentication can supply a `fetch` function that adds an existing
short-lived credential; credential storage and renewal belong to the host.
Do not embed long-lived secrets in browser source or use a privileged account
merely to display a status. Normal viewer permissions still apply.
