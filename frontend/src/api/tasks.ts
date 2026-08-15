const BASE = "/api";

export interface TaskSummary {
  task_id: string;
  task_name: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
}

// Extends TaskSummary honestly now: GET /api/tasks/{id} really does return
// started_at/finished_at (it previously didn't, so this type claimed two
// fields that were always undefined at runtime), plus the operational detail
// below that TaskRun has always stored but the endpoint never sent.
export interface TaskDetail extends TaskSummary {
  args: string;
  kwargs: string;
  exception: string | null;
  traceback: string | null;
  log_output: string | null;
  queue: string | null;
  worker: string | null;
  runtime_seconds: number | null;
}

export async function listTasks(params: { status?: string; task_name?: string } = {}): Promise<TaskSummary[]> {
  const query = new URLSearchParams(params as Record<string, string>).toString();
  const response = await fetch(`${BASE}/tasks${query ? `?${query}` : ""}`, { credentials: "include" });
  if (!response.ok) throw new Error("Couldn't load tasks.");
  return response.json();
}

export async function getTask(taskId: string): Promise<TaskDetail> {
  const response = await fetch(`${BASE}/tasks/${taskId}`, { credentials: "include" });
  if (!response.ok) throw new Error(response.status === 404 ? "Task not found." : "Couldn't load this task.");
  return response.json();
}

export async function retryTask(taskId: string): Promise<{ new_task_id: string }> {
  const response = await fetch(`${BASE}/tasks/${taskId}/retry`, { method: "POST", credentials: "include" });
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
