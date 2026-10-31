# Moving from Flower

celery-manager can replace Flower's dashboard; the scripts and Grafana JSON
data sources around Flower are usually what makes switching expensive. Three
read-only endpoints answer in Flower's shapes, so those keep working with only
a base-URL change:

| Flower | celery-manager | Notes |
|---|---|---|
| `GET /api/tasks` | `GET /api/flower/tasks` | `{task_id: task}`; `limit` (max 1000), `offset`, `sort_by` (`name`, `state`, `received`, `started`, prefix `-` to reverse), `workername`, `taskname`, `state` (`SUCCESS`, `FAILURE`, `STARTED`, ...), `received_start` / `received_end` (ISO 8601 or Unix seconds). |
| `GET /api/task/info/<id>` | `GET /api/flower/task/info/<id>` | One task; 404 when unknown. |
| `GET /api/workers?status=true` | `GET /api/flower/workers?status=true` | `{hostname: online}`; without `status`, the last observed state, active and processed counts. |

Each is also under `/api/v1/flower/...`. Times are Unix seconds, as in Flower.
States map from celery-manager's statuses: `queued` → `PENDING`, `received` →
`RECEIVED`, `started` → `STARTED`, `succeeded` → `SUCCESS`, `failed` and
`interrupted` → `FAILURE`, `retried` → `RETRY`, `revoked` and `skipped` →
`REVOKED`.

Differences to plan for:

- **Authentication.** Requests need a celery-manager session cookie or, with an
  OIDC provider configured, the host's bearer token. Flower's basic auth is not
  accepted.
- **History, not memory.** Data comes from celery-manager's database, so it
  survives restarts and is bounded by [retention](retention.md), not by Flower's
  in-memory task limit.
- **Read-only.** Flower's control endpoints (revoke, rate limit, pool
  grow/shrink, queue consumers) are not mirrored. Use the audited
  [REST API](rest-api.md) routes, which check roles and dispatch policy.
- **Tenant mode.** With `tenant_resolver` configured these global views are
  refused (403), like every other global view.
- **Metrics.** Flower's `/metrics` is not mirrored; celery-manager's own
  [`/metrics`](metrics.md) has different names, and ships a ready Grafana
  dashboard.
