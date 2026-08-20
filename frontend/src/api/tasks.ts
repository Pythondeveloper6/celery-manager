const BASE = "/api";

// Every datetime below arrives as an ISO string carrying an explicit UTC
// offset. That is load-bearing, not incidental: SQLite drops tzinfo on
// round-trip, so the API stamps UTC back on at the boundary (see `_utc` in
// api/routes/tasks.py). Without it `new Date(value)` parses a bare
// "2026-08-10T00:00:00" as *local* time, which would make the live elapsed
// counters below wrong by the viewer's UTC offset — or negative.
export interface TaskSummary {
  task_id: string;
  task_name: string;
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
  traceback: string | null;
  log_output: string | null;
  queue: string | null;
  worker: string | null;
  eta: string | null;
  // Both directions of the retry chain. `retried_by` is a list because an
  // operator can retry the same failed run more than once.
  retried_from_id: string | null;
  retried_by: string[];
}

export interface TaskPage {
  items: TaskSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface TaskQuery {
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

export async function getTask(taskId: string): Promise<TaskDetail> {
  const response = await fetch(`${BASE}/tasks/${encodeURIComponent(taskId)}`, { credentials: "include" });
  if (!response.ok) throw new Error(response.status === 404 ? "Task not found." : "Couldn't load this task.");
  return response.json();
}

export async function retryTask(taskId: string): Promise<{ new_task_id: string }> {
  const response = await fetch(`${BASE}/tasks/${encodeURIComponent(taskId)}/retry`, {
    method: "POST",
    credentials: "include",
  });
  if (!response.ok) throw new Error("Retry failed.");
  return response.json();
}

export async function bulkRetryTasks(taskIds: string[]): Promise<{ new_task_ids: string[] }> {
  const response = await fetch(`${BASE}/tasks/retry-bulk`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ task_ids: taskIds }),
  });
  if (!response.ok) throw new Error("Bulk retry failed.");
  return response.json();
}

export interface TaskEvent {
  task_id: string;
  type: string;
}

export function subscribeToUpdates(onEvent: (event: TaskEvent) => void): () => void {
  const source = new EventSource(`${BASE}/stream`, { withCredentials: true });
  source.onmessage = (message) => onEvent(JSON.parse(message.data));
  return () => source.close();
}
