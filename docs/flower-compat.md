# Moving from Flower

celery-manager can replace Flower's dashboard; the scripts and Grafana JSON
data sources around Flower are usually what makes switching expensive. These
endpoints answer in Flower's shapes, so those keep working with only a
base-URL and credential change:

| Flower | celery-manager | Notes |
|---|---|---|
| `GET /api/tasks` | `GET /api/flower/tasks` | `{task_id: task}`; `limit` (max 1000), `offset`, `sort_by` (`name`, `state`, `received`, `started`, prefix `-` to reverse), `workername`, `taskname`, `state` (`SUCCESS`, `FAILURE`, `STARTED`, ...), `received_start` / `received_end` (ISO 8601 or Unix seconds). |
| `GET /api/task/info/<id>` | `GET /api/flower/task/info/<id>` | One task; 404 when unknown. |
| `GET /api/workers?status=true` | `GET /api/flower/workers?status=true` | `{hostname: online}`; without `status`, the last observed state, active and processed counts. |
| `GET /api/queues/length` | `GET /api/flower/queues/length` | `{"active_queues": [{"name", "messages"}]}`. |
| `POST /api/task/revoke/<id>` | `POST /api/flower/task/revoke/<id>` | `terminate`, `signal` (`SIGKILL` needs admin). Operator role; audited. |
| `POST /api/task/async-apply/<name>` | `POST /api/flower/task/async-apply/<name>` | Body `{"args", "kwargs", "countdown"}`; the dispatch allowlist applies. Returns `{"task-id", "state"}`. |
| `POST /api/worker/pool/grow/<name>` | `POST /api/flower/worker/pool/grow/<name>` | `n` (default 1). Operator role; audited. |
| `POST /api/worker/pool/shrink/<name>` | `POST /api/flower/worker/pool/shrink/<name>` | As grow. |

Each is also under `/api/v1/flower/...`. Times are Unix seconds, as in Flower.
States map from celery-manager's statuses: `queued` → `PENDING`, `received` →
`RECEIVED`, `started` → `STARTED`, `succeeded` → `SUCCESS`, `failed` and
`interrupted` → `FAILURE`, `retried` → `RETRY`, `revoked` and `skipped` →
`REVOKED`.

Differences to plan for:

- **Authentication.** Requests need a celery-manager session cookie, the
  host's bearer token, or an API token for scripts: an admin creates one with
  `POST /api/tokens` (`{"name", "role", "expires_at"}`; the token is returned
  once) and revokes it with `DELETE /api/tokens/<id>`. Send it as
  `Authorization: Bearer cmk_...`; its actions are audited as `token:<name>`.
  Tokens are refused when `tenant_resolver` is configured. Flower's basic auth
  is not accepted.
- **History, not memory.** Data comes from celery-manager's database, so it
  survives restarts and is bounded by [retention](retention.md), not by Flower's
  in-memory task limit.
- **Other controls.** Flower's remaining control endpoints are not mirrored.
  Use the audited [REST API](rest-api.md) routes, which include
  `POST /api/workers/<name>/pool-restart` (admin; the worker needs
  `worker_pool_restarts = True`), `POST /api/workers/<name>/autoscale`
  (`{"max", "min"}`) and `POST /api/tasks/time-limit`
  (`{"task_name", "soft", "hard"}`).
- **Tenant mode.** With `tenant_resolver` configured these global views are
  refused (403), like every other global view.
- **Metrics.** Flower's `/metrics` is not mirrored; celery-manager's own
  [`/metrics`](metrics.md) has different names, and ships a ready Grafana
  dashboard.
