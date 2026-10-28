import { API_BASE as BASE } from "./base";
import { describeFailure } from "./settings";

/** Days to keep, or null to keep forever. */
export type Days = number | null;

export interface LastPrune {
  at: string;
  by: string;
  dry_run: boolean;
  runs_deleted: number;
  audit_deleted: number;
  by_task: Record<string, number>;
  tasks_truncated?: boolean;
}

export interface RetentionRules {
  default_days: Days;
  failed_days: Days;
  audit_days: Days;
  // A null value is an explicit "keep this task forever", whatever the default says.
  tasks: Record<string, Days>;
}

export interface RetentionSettings extends RetentionRules {
  known_tasks: string[];
  last_prune: LastPrune | null;
}

export interface RetentionPreview {
  runs: number;
  audit_log: number;
  by_task: Record<string, number>;
}

async function send<T>(path: string, method: string, body: unknown, fallback: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}/settings/retention${path}`, {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      credentials: "include",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new Error("Couldn't reach the server.");
  }
  if (!response.ok) {
    const message = response.status === 401 ? "Your session has expired. Sign in again." : fallback;
    throw new Error(await describeFailure(response, message));
  }
  return response.json();
}

export function getRetention(): Promise<RetentionSettings> {
  return send("", "GET", undefined, "Couldn't load the retention settings.");
}

export function saveRetention(rules: RetentionRules): Promise<RetentionSettings> {
  return send("", "PUT", rules, "Couldn't save the retention settings.");
}

/** What a prune would delete right now if `rules` were saved. Deletes nothing. */
export function previewRetention(rules: RetentionRules): Promise<RetentionPreview> {
  return send("/preview", "POST", rules, "Couldn't preview the retention settings.");
}
