import { API_BASE as BASE } from "./base";

// Tasks that left the queue but haven't run: delayed ones a worker holds
// until their ETA (control/workers.py), and messages a worker took on Redis
// but never acknowledged (control/unacked.py).

// Types and keyword names only; values are never sent unless revealed.
export interface MaskedCall {
  args: string[];
  kwargs: Record<string, string>;
}

export interface DelayedTask {
  task_id: string;
  task_name: string | null;
  // UTC ISO 8601.
  eta: string | null;
  worker: string;
  queue: string | null;
  priority: number | null;
  args_masked: MaskedCall;
  // Only with reveal (operators); null otherwise.
  args: unknown[] | null;
  kwargs: Record<string, unknown> | null;
  // What task_runs knows, if it has the run at all.
  run: { status: string; schedule_name: string | null } | null;
}

export interface UnheldRun {
  task_id: string;
  task_name: string;
  eta: string;
  queue: string | null;
}

export interface DelayedListing {
  tasks: DelayedTask[];
  workers_replied: string[];
  unheld_runs: UnheldRun[];
  unheld_truncated: boolean;
  args_revealed: boolean;
  timeout_seconds: number;
  inspected_at: string;
}

export interface CancelDelayedResult {
  revoked: number;
  runs_marked_revoked: number;
}

export type UnackedState = "running" | "held" | "orphaned" | "unknown";

export interface UnackedEntry {
  tag: string;
  task_id: string | null;
  task_name: string | null;
  queue: string | null;
  exchange: string | null;
  eta: string | null;
  redelivered: boolean;
  held_since: string;
  age_seconds: number;
  past_visibility_timeout: boolean;
  message_missing: boolean;
  run_status: string | null;
  state: UnackedState;
  worker: string | null;
  worker_state: "active" | "reserved" | "scheduled" | null;
}

export type UnackedListing =
  | { supported: false; reason: string; transport: string | null; inspected_at: string }
  | {
      supported: true;
      reason: null;
      transport: string;
      visibility_timeout_seconds: number;
      total: number;
      entries: UnackedEntry[];
      keys: { unacked: string; unacked_index: string; prefix: string };
      workers_replied: string[] | null;
      workers_error: string | null;
      inspected_at: string;
    };

export interface RestoreResult {
  restored: { tag: string; queue: string; task_id: string | null }[];
  missing: string[];
  refused: { tag: string; task_id: string | null; reason: string }[];
}

// The server's `detail` says what to do next (a 503 names the broker, a 409
// says why nothing was restored, a 403 names the role).
async function describeFailure(response: Response, fallback: string): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (body && typeof body === "object" && "detail" in body) {
      const detail = (body as { detail: unknown }).detail;
      if (typeof detail === "string" && detail.trim()) return detail.trim();
    }
  } catch {
    // Not JSON. Fall through.
  }
  return fallback;
}

async function post<T>(path: string, body: unknown, fallback: string): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(await describeFailure(response, fallback));
  return response.json();
}

export async function listDelayed(revealArgs = false): Promise<DelayedListing> {
  const query = revealArgs ? "?reveal_args=true" : "";
  const response = await fetch(`${BASE}/delayed${query}`, { credentials: "include" });
  if (!response.ok) throw new Error(await describeFailure(response, "Couldn't load delayed tasks."));
  return response.json();
}

export function cancelDelayed(taskIds: string[]): Promise<CancelDelayedResult> {
  return post("/delayed/cancel", { task_ids: taskIds }, "Couldn't cancel.");
}

export async function listUnacked(): Promise<UnackedListing> {
  const response = await fetch(`${BASE}/queues/unacked`, { credentials: "include" });
  if (!response.ok) throw new Error(await describeFailure(response, "Couldn't load unacknowledged messages."));
  return response.json();
}

// `force` also restores messages a live worker reports reserved/scheduled.
// The server never restores one a worker reports running.
export function restoreUnacked(tags: string[], force = false): Promise<RestoreResult> {
  return post("/queues/unacked/restore", { tags, force }, "Couldn't restore.");
}

// Mirrors MAX_BULK_CANCEL (control/tasks.py) and MAX_RESTORE (control/unacked.py).
export const MAX_DELAYED_CANCEL = 1000;
export const MAX_RESTORE = 500;
