import { API_BASE as BASE, forbiddenMessage } from "./base";
import { errorMessage } from "./tasks";

// Runs that failed for good: status `failed`, and nothing re-run from them.
// See docs/dead-letters.md. Dates arrive as ISO strings with a UTC offset.
export interface DeadLetter {
  task_id: string;
  task_name: string;
  exception: string | null;
  exception_type: string;
  // The line that names the error: last line of the traceback.
  last_line: string | null;
  // As stored on the run: JSON text, or a Python repr for older rows.
  args: string;
  kwargs: string;
  queue: string | null;
  // Runs that came before this one in its retry chain.
  earlier_attempts: number;
  started_at: string | null;
  finished_at: string | null;
}

export interface DeadLetterGroup {
  task_name: string;
  exception_type: string;
  count: number;
}

export interface DeadLetterSummary {
  groups: DeadLetterGroup[];
  total: number;
  // True when the summary covers only the newest dead letters.
  truncated: boolean;
}

export interface DeadLetterPage {
  items: DeadLetter[];
  total: number;
  limit: number;
  offset: number;
  summary: DeadLetterSummary;
}

export interface DeadLetterQuery {
  task_name?: string;
  exception?: string;
  since?: string;
  limit?: number;
  offset?: number;
}

export async function listDeadLetters(params: DeadLetterQuery = {}): Promise<DeadLetterPage> {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    query.set(key, String(value));
  }
  const suffix = query.toString();
  const response = await fetch(`${BASE}/dead-letters${suffix ? `?${suffix}` : ""}`, { credentials: "include" });
  if (!response.ok) throw new Error("Couldn't load the dead letters.");
  return response.json();
}

export interface RerunResult {
  reruns: { task_id: string; new_task_id: string; args_edited: boolean }[];
  // Runs the broker refused; they are still in the list.
  errors: { task_id: string; error: string }[];
}

// Leave `args`/`kwargs` out to run each again with its own arguments. Given,
// they replace the stored ones for every id, which the server only accepts
// when all the ids are runs of one task.
export async function rerunDeadLetters(
  taskIds: string[],
  overrides: { args?: unknown[]; kwargs?: Record<string, unknown> } = {},
): Promise<RerunResult> {
  const response = await fetch(`${BASE}/dead-letters/rerun`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ task_ids: taskIds, ...overrides }),
  });
  if (response.status === 403) throw new Error(await forbiddenMessage(response));
  if (!response.ok) throw new Error(await errorMessage(response, "Couldn't run them again."));
  return response.json();
}
