# Tenant-scoped task history

`tenant_resolver` opts one dashboard application into **read-only tenant history
mode**. It is a security boundary, separate from the convenience “Started by me”
filter. With it enabled, even an admin can read only records assigned to the
resolved tenant. Records with no tenant ID are invisible. Without it, existing
manager-wide behavior remains unchanged.

```python
from celery_manager import CeleryManagerConfig
from celery_manager.auth.external import ExternalUser

# These values must come from verified host authentication and membership.
# This example assumes trusted host middleware populated request.state.
def authenticate(request):
    user = request.state.verified_user
    return ExternalUser(user.username, "viewer", subject=user.subject)

async def tenant_for(request, principal):
    membership = await host_memberships.active_for(principal.subject)
    return membership.tenant_id if membership else None

config = CeleryManagerConfig(
    ...,
    authenticate_request=authenticate,
    tenant_resolver=tenant_for,
)
```

The resolver receives `(request, principal)` and may be synchronous or asynchronous.
It must return a nonempty printable tenant ID of at most 255 UTF-16 code units, or `None`
to deny access. Exceptions fail closed with 503; a missing or invalid scope is
403. Basic characters use one code unit; supplementary characters such as emoji
use two. This portable bound matches SQL Server storage and is enforced on all
backends before tenant writes or scope resolution. Ordinary authentication still happens first. Do not derive the tenant from
an unverified query parameter, cookie or header. If the host supports a tenant
selector, validate the authenticated person's membership before returning its
canonical ID. The package does not trust `?tenant_id=...` and does not implement
membership management.

Tenant identity is exact: case, accents, Unicode normalization forms and trailing
spaces remain distinct even when the database's default text collation treats
them as equal. The resolver does not trim or normalize its returned ID. Return
the same stable canonical ID used by trusted producers; if the host normalizes
IDs, do so consistently before both assignment and resolution. All-whitespace
IDs are refused as HTTP scope. SQL Server stores these identifiers as Unicode,
and MySQL uses `utf8mb4` storage for them.

## Admitted reads

- Task lists and their counts, with tenant filtering applied in SQL before
  ordering, paging or export limits.
- Individual task details, including stored logs/results and progress, subject to
  the existing log-role restriction. A guessed foreign or unassigned task ID
  returns 404, just like a missing task.
- CSV/JSON task exports, including the streaming body and response counts.
- Failure inbox summaries and failure review history. Foreign review history
  returns an empty list; history whose task has been pruned is not exposed.
- Related attempts match the selected row's operation/correlation key and tenant;
  workflow nodes and retry lineage are restricted to visible records. Foreign
  parent/root/retry references are suppressed; workflow group identifiers are
  withheld. The displayed root can be the current run when the original root is
  outside the tenant.
- The current identity and public sign-in options. The current identity includes
  its trusted `tenant_id`; capabilities advertise `tenant_scoped=true`,
  `read_only=true`, and polling instead of an event stream.

The same restrictions apply to `/api` and `/api/v1`. SQL scoping is bound to the
HTTP request context and the manager's session factory. Direct worker/listener
sessions remain global so observation collection is unaffected. Producer-side
assignment of `TaskRun.tenant_id` must use trusted host code, such as the
[non-Celery observation API](real-world-operations.md#observe-host-owned-work); request scope does not retroactively
assign existing history or infer a tenant from task arguments.

## Deliberately denied operations

All operational writes are refused with 403, including starting, retrying,
cancelling or terminating tasks, bulk actions, failure acknowledgement and reruns.
Tenant mode never sends Celery controls for a tenant's task IDs. Global broker,
queue, worker, schedule, settings, account-management, alert, audit, health and
analytics APIs are denied. Metrics, OpenAPI/docs endpoints and SSE/WebSocket
channels are also denied; the UI uses scoped list polling. Session-list and
revocation APIs are outside this mode's allowlist. Local login/logout remain
available, but do not grant unscoped history access.

The dashboard labels the resolved tenant and read-only mode, hides operational
controls and limits navigation. These UI affordances are not the enforcement:
the server rejects direct requests as well. New endpoints are denied unless
explicitly reviewed and admitted to the tenant-history allowlist.

For manager-wide administration, deploy a separate application/configuration
without `tenant_resolver`, protected by an appropriate manager-only host
permission and deployment boundary. There is no `admin` role bypass and no
query/header switch to disable isolation. Sharing a database does not make a
tenant-facing app an administrative console.

## Scope and rollout limits

This mode isolates retained task observations, not the broker, scheduler,
application payloads or execution resources. Trusted producers are responsible
for assigning the correct tenant to each record. A task's own arguments, logs,
results and context can still contain business data; review what the host writes
and who may read it. Tenants intentionally sharing an ID intentionally share
history. Use separate database/schema credentials when stronger storage isolation
is required.

Local tests cover two tenants, unassigned records, counts/paging, streaming
exports, guessed IDs, workflow references, failure summaries/history, concurrent
request contexts, denied admin writes and denied global/event surfaces. Those
checks do not replace a rollout trial against the host's authentication,
membership lifecycle, mounting and chosen production SQL backend.
