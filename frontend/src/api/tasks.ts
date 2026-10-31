import { API_BASE as BASE, forbiddenMessage } from "./base";

// Every datetime below arrives as an ISO string carrying an explicit UTC
// offset. That is load-bearing, not incidental: SQLite drops tzinfo on
// round-trip, so the API stamps UTC back on at the boundary (see `_utc` in
// api/routes/tasks.py). Without it `new Date(value)` parses a bare
// "2026-08-10T00:00:00" as *local* time, which would make the live elapsed
// counters below wrong by the viewer's UTC offset — or negative.
export interface TaskSummary {
  task_id: string;
  task_name: string;
  source: string;
  tenant_id: string | null;
  operation_key: string | null;
  correlation_id: string | null;
  context_json: string | null;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  runtime_seconds: number | null;
  // Progress is opt-in: only tasks whose body calls
  // `celery_manager.report_progress()` have any. null therefore means "this
  // task never reported progress", which the UI must render differently from
  // "0% done" — a task at 0% looks stuck, a task with no progress reporting
  // is simply not instrumented.
  progress_current: number | null;
  progress_total: number | null;
  progress_message: string | null;
  progress_updated_at: string | null;
}

export interface TaskDetail extends TaskSummary {
  args: string;
  kwargs: string;
  exception: string | null;
  // Why a skipped run was skipped ("schedule paused", "maintenance window:
  // <name>"); null for other statuses and for skips recorded before it existed.
  skip_reason: string | null;
  traceback: string | null;
  log_output: string | null;
  log_output_truncated: boolean;
  logs_restricted: boolean;
  result_repr: string | null;
  result_truncated: boolean;
  initiator: string | null;
  queue: string | null;
  worker: string | null;
  eta: string | null;
  // From the task-sent / task-received events; null when never seen.
  sent_at: string | null;
  received_at: string | null;
  // started_at - max(sent_at, eta): time spent waiting in the queue.
  wait_seconds: number | null;
  // Both directions of the retry chain. `retried_by` is a list because an
  // operator can retry the same failed run more than once.
  retried_from_id: string | null;
  retried_by: string[];
  // Workflow lineage (chain/group/chord) from Celery; null for a standalone run.
  parent_id: string | null;
  root_id: string | null;
  group_id: string | null;
}

export interface TaskPage {
  items: TaskSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface TaskQuery {
  started_by_me?: boolean;
  initiator?: string;
  status?: string;
  task_name?: string;
  q?: string;
  date_from?: string;
  date_to?: string;
  limit?: number;
  offset?: number;
}

export async function listTasks(params: TaskQuery = {}): Promise<TaskPage> {
  // Built key-by-key rather than by casting the object: an undefined or empty
  // value must be *absent* from the query string, not sent as the literal
  // "undefined", which the API would treat as a real filter and match nothing.
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    query.set(key, String(value));
  }
  const suffix = query.toString();
  const response = await fetch(`${BASE}/tasks${suffix ? `?${suffix}` : ""}`, { credentials: "include" });
  if (!response.ok) throw new Error("Couldn't load tasks.");
  return response.json();
}

export class TaskNotRecorded extends Error {}

export async function getTask(taskId: string): Promise<TaskDetail> {
  const response = await fetch(`${BASE}/tasks/${encodeURIComponent(taskId)}`, { credentials: "include" });
  if (response.status === 404) throw new TaskNotRecorded("Waiting for a task record. Check the task ID if it does not appear.");
  if (!response.ok) throw new Error("Couldn't load this task.");
  return response.json();
}

// FastAPI reports validation failures as `detail: [{loc, msg}]` and other
// refusals as `detail: "text"`; either way the operator should see words.
export async function errorMessage(response: Response, fallback: string): Promise<string> {
  try {
    const body = await response.json();
    const detail = body?.detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail) && detail.length > 0) {
      return detail
        .map((d: { loc?: unknown[]; msg?: string }) => `${(d.loc ?? []).slice(1).join(".")}: ${d.msg}`)
        .join("; ");
    }
  } catch {
    // not JSON; fall through
  }
  return fallback;
}

// With no overrides this is a plain retry with the stored arguments. Passing
// `args` and/or `kwargs` re-dispatches with those instead (and is audited as
// an edited retry); a field left undefined keeps the stored value.
export async function retryTask(
  taskId: string,
  overrides?: { args?: unknown[]; kwargs?: Record<string, unknown> },
): Promise<{ new_task_id: string }> {
  const response = await fetch(`${BASE}/tasks/${encodeURIComponent(taskId)}/retry`, {
    method: "POST",
    credentials: "include",
    ...(overrides ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify(overrides) } : {}),
  });
  if (!response.ok) throw new Error(await errorMessage(response, "Retry failed."));
  return response.json();
}

export interface SignatureParam {
  name: string;
  kind: "positional" | "keyword_only" | "var_args" | "var_kwargs";
  required: boolean;
  has_default?: boolean;
  default: unknown;
  annotation: string | null;
}

export interface RegisteredTask {
  name: string;
  // null when the task's signature could not be inspected.
  signature: SignatureParam[] | null;
  queue: string | null;
  doc: string | null;
  // JSON Schema of the task's `args_schemas` model, when one is configured.
  args_schema?: Record<string, unknown> | null;
}

export async function listRegisteredTasks(): Promise<RegisteredTask[]> {
  const response = await fetch(`${BASE}/tasks/registered`, { credentials: "include" });
  if (!response.ok) throw new Error("Couldn't load the registered tasks.");
  return (await response.json()).items;
}

export interface StartTaskRequest {
  task_name: string;
  args: unknown[];
  kwargs: Record<string, unknown>;
  queue?: string;
  countdown?: number;
  eta?: string;
}

export async function startTask(request: StartTaskRequest): Promise<{ task_id: string }> {
  const response = await fetch(`${BASE}/tasks/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify(request),
  });
  if (!response.ok) throw new Error(await errorMessage(response, "Couldn't start the task."));
  return response.json();
}

// `terminate=false` is a plain revoke: a queued task is skipped once a worker
// reaches it, but one already running is left to finish. `terminate=true`
// additionally asks the worker to kill the child process executing it.
export async function cancelTask(taskId: string, terminate = false): Promise<void> {
  const response = await fetch(`${BASE}/tasks/${encodeURIComponent(taskId)}/cancel${terminate ? "?terminate=true" : ""}`, {
    method: "POST",
    credentials: "include",
  });
  if (response.status === 403) throw new Error(await forbiddenMessage(response));
  if (!response.ok) throw new Error(terminate ? "Terminate failed." : "Cancel failed.");
}

export async function bulkRetryTasks(taskIds: string[]): Promise<{ new_task_ids: string[] }> {
  const response = await fetch(`${BASE}/tasks/retry-bulk`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    // confirm_count echoes the scope the operator approved; the server refuses
    // the batch if it differs (and requires it with require_typed_confirmation).
    body: JSON.stringify({ task_ids: taskIds, confirm_count: taskIds.length }),
  });
  if (response.status === 403) throw new Error(await forbiddenMessage(response));
  if (!response.ok) throw new Error("Bulk retry failed.");
  return response.json();
}

// Bulk cancel takes either explicit ids or the list's own filter fields, never
// both. `dry_run` returns the counts without revoking — used to show "Cancel
// 37 matching" before the operator confirms. Over the server's cap (1,000)
// the whole request is refused and `detail` says why.
export interface BulkCancelFilter {
  started_by_me?: boolean;
  initiator?: string;
  status?: string;
  task_name?: string;
  q?: string;
  date_from?: string;
  date_to?: string;
}

export interface BulkCancelResult {
  revoked: number;
  matched: number;
  skipped_finished: number;
  unknown: number;
  limit: number;
  dry_run: boolean;
}

export async function bulkCancelTasks(
  selector: { task_ids: string[] } | { filter: BulkCancelFilter },
  options: { terminate?: boolean; dry_run?: boolean } = {}
): Promise<BulkCancelResult> {
  const response = await fetch(`${BASE}/tasks/cancel-bulk`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ ...selector, terminate: options.terminate ?? false, dry_run: options.dry_run ?? false }),
  });
  if (!response.ok) {
    let message = "Bulk cancel failed.";
    try {
      const body = await response.json();
      if (typeof body?.detail === "string") message = body.detail;
    } catch {
      // keep the generic message
    }
    throw new Error(message);
  }
  return response.json();
}

/** One changed `task_runs` row, as the API's change feed saw it. The feed
 *  hears every writer — the events listener and workers included, which run
 *  in other processes — because it polls the database, not an in-memory bus. */
export interface TaskEvent {
  task_id: string;
  type: string;
  status?: string;
  updated_at?: string;
}

/** "connecting" until the first open, "connected" while the stream is up,
 *  "reconnecting" from the moment it drops until it is back. Callers use it
 *  to decide whether to poll instead, and to show an honest "Live" badge. */
export type StreamState = "connecting" | "connected" | "reconnecting" | "polling";

// How long to wait before rebuilding a stream the browser gave up on.
const RECONNECT_DELAY_MS = 5000;

export function subscribeToUpdates(
  onEvent: (event: TaskEvent) => void,
  onState?: (state: StreamState) => void
): () => void {
  let source: EventSource | null = null;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let closed = false;

  const connect = () => {
    source = new EventSource(`${BASE}/stream`, { withCredentials: true });
    source.onopen = () => onState?.("connected");
    source.onerror = () => {
      onState?.("reconnecting");
      // EventSource retries a dropped connection by itself, but gives up for
      // good (readyState CLOSED) on a non-200 answer — a restart's 502, an
      // expired session's 401. Rebuild it ourselves after a pause, so a
      // dashboard left open recovers instead of polling forever.
      if (source?.readyState === EventSource.CLOSED && !closed) {
        source.close();
        retryTimer = setTimeout(connect, RECONNECT_DELAY_MS);
      }
    };
    source.onmessage = (message) => {
      // A frame that isn't JSON — a proxy injecting a keep-alive comment as
      // data, a truncated write — used to throw straight out of the event
      // handler as an uncaught error. One bad frame is not a reason to make
      // noise in the console of a dashboard left open all day; the next good
      // frame refreshes the list anyway.
      let event: TaskEvent;
      try {
        event = JSON.parse(message.data);
      } catch {
        return;
      }
      onEvent(event);
    };
  };

  onState?.("connecting");
  fetch(`${BASE}/auth/options`, { credentials: "include" })
    .then((response) => response.json())
    .then((options) => {
      if (closed) return;
      if (options.live_stream === "poll") onState?.("polling");
      else connect();
    })
    .catch(() => { if (!closed) connect(); });
  return () => {
    closed = true;
    if (retryTimer !== null) clearTimeout(retryTimer);
    source?.close();
  };
}
