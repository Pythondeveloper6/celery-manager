import { API_BASE as BASE } from "./base";

// Live worker state, straight from Celery's broadcast `inspect()` — nothing
// here is stored by celery-manager. See control/workers.py.

export interface WorkerTaskRef {
  id: string | null;
  name: string | null;
  // Unix seconds (a float), as the worker reports it. Active tasks only.
  time_start?: number | null;
  // Scheduled (countdown/ETA) tasks only.
  eta?: string | null;
}

export interface Worker {
  name: string;
  online: boolean;
  pid: number | null;
  uptime_seconds: number | null;
  // Short pool name ("prefork", "threads", "solo", "gevent"...), or null if
  // the worker didn't answer `stats`.
  pool: string | null;
  concurrency: number | null;
  pool_processes: number | null;
  processed_total: number;
  processed_by_task: Record<string, number>;
  active_count: number;
  reserved_count: number;
  scheduled_count: number;
  active: WorkerTaskRef[];
  reserved: WorkerTaskRef[];
  scheduled: WorkerTaskRef[];
  queues: string[];
}

export interface WorkerSnapshot {
  workers: Worker[];
  timeout_seconds: number;
  inspected_at: string;
}

// Who answered a control broadcast. A worker missing from both lists didn't
// reply within the timeout — the command may still have reached it.
export interface ControlResult {
  acknowledged: string[];
  errors: Record<string, string>;
}

export interface TaskRateLimit {
  task_name: string;
  // The limit declared in code (`@app.task(rate_limit=...)`), not whatever a
  // worker is enforcing after a runtime change. null = unlimited.
  rate_limit: string | null;
}

// Prefer the server's `detail` string: the 503 for an unreachable broker and
// the 422 for a malformed rate both say what to do next.
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
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw new Error(await describeFailure(response, fallback));
  return response.json();
}

export async function listWorkers(): Promise<WorkerSnapshot> {
  const response = await fetch(`${BASE}/workers`, { credentials: "include" });
  if (!response.ok) throw new Error(await describeFailure(response, "Couldn't load workers."));
  return response.json();
}

const worker = (name: string) => `/workers/${encodeURIComponent(name)}`;

export function shutdownWorker(name: string): Promise<{ ok: boolean }> {
  return post(`${worker(name)}/shutdown`, undefined, "Couldn't send the shutdown command.");
}

export function growPool(name: string, n = 1): Promise<ControlResult> {
  return post(`${worker(name)}/pool/grow`, { n }, "Couldn't grow the pool.");
}

export function shrinkPool(name: string, n = 1): Promise<ControlResult> {
  return post(`${worker(name)}/pool/shrink`, { n }, "Couldn't shrink the pool.");
}

export function addConsumer(name: string, queue: string): Promise<ControlResult> {
  return post(`${worker(name)}/queues/add`, { queue }, "Couldn't add the queue.");
}

export function cancelConsumer(name: string, queue: string): Promise<ControlResult> {
  return post(`${worker(name)}/queues/cancel`, { queue }, "Couldn't stop consuming the queue.");
}

export async function listRateLimits(): Promise<TaskRateLimit[]> {
  const response = await fetch(`${BASE}/workers/rate-limits`, { credentials: "include" });
  if (!response.ok) throw new Error(await describeFailure(response, "Couldn't load registered tasks."));
  return response.json();
}

// `rateLimit` "" removes the limit. Not persisted: workers forget it on restart.
export function setRateLimit(taskName: string, rateLimit: string, destination?: string[]): Promise<ControlResult> {
  return post(
    "/tasks/rate-limit",
    { task_name: taskName, rate_limit: rateLimit, destination: destination ?? null },
    "Couldn't change the rate limit.",
  );
}

// Same rule the server enforces; checked here so a typo is caught before a
// round trip.
export const RATE_LIMIT_PATTERN = /^\d+(\.\d+)?(\/[smh])?$/;
