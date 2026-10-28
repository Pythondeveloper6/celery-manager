import { API_BASE as BASE } from "./base";
import type { TaskSummary } from "./tasks";

export type RangeKey = "1h" | "24h" | "7d" | "30d";
export const RANGE_KEYS: RangeKey[] = ["1h", "24h", "7d", "30d"];

export interface Bucket {
  start: string;
  counts: Record<string, number>;
  total: number;
  success_rate: number | null;
  runtime_p50: number | null;
  runtime_p95: number | null;
}

export interface TaskRollup {
  task_name: string;
  runs: number;
  failed: number;
  failure_rate: number;
}

export interface Overview {
  range: RangeKey;
  task_name: string | null;
  since: string;
  until: string;
  bucket_seconds: number;
  // True when the window held more runs than the server scans; the figures
  // then describe the newest runs only.
  truncated: boolean;
  totals: {
    runs: number;
    by_status: Record<string, number>;
    succeeded: number;
    failed: number;
    success_rate: number | null;
    runtime_p50: number | null;
    runtime_p95: number | null;
    throughput_per_hour: number;
  };
  buckets: Bucket[];
  top_by_volume: TaskRollup[];
  top_by_failure_rate: TaskRollup[];
}

export interface FailureGroup {
  signature: string;
  exception_type: string;
  message: string;
  frame: string | null;
  example: string;
  count: number;
  task_names: string[];
  first_seen: string | null;
  last_seen: string | null;
  example_task_ids: string[];
  // Capped server-side; `task_ids_truncated` says whether it covers the group.
  task_ids: string[];
  task_ids_truncated: boolean;
}

export interface Failures {
  range: RangeKey;
  truncated: boolean;
  total_failures: number;
  groups: FailureGroup[];
}

export interface TaskStats {
  task_name: string;
  range: RangeKey;
  truncated: boolean;
  totals: { runs: number; by_status: Record<string, number>; succeeded: number; failed: number; success_rate: number | null };
  runtime: { p50: number | null; p95: number | null; max: number | null };
  // Queue wait of runs that started in the range; null without recorded send times.
  wait: { runs: number; p50: number; p95: number; max: number } | null;
  last_success_at: string | null;
  last_failure_at: string | null;
  recent_runs: TaskSummary[];
  top_errors: FailureGroup[];
}

async function get<T>(path: string, params: Record<string, string | undefined>, failure: string): Promise<T> {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value) query.set(key, value);
  }
  const response = await fetch(`${BASE}/analytics/${path}?${query.toString()}`, { credentials: "include" });
  if (!response.ok) throw new Error(response.status === 404 ? "No runs recorded for this task." : failure);
  return response.json();
}

export const getOverview = (range: RangeKey, taskName?: string) =>
  get<Overview>("overview", { range, task_name: taskName }, "Couldn't load the trends.");

export const getFailures = (range: RangeKey, taskName?: string) =>
  get<Failures>("failures", { range, task_name: taskName }, "Couldn't load the failures.");

export const getTaskStats = (taskName: string, range: RangeKey) =>
  get<TaskStats>(`tasks/${encodeURIComponent(taskName)}`, { range }, "Couldn't load this task.");
