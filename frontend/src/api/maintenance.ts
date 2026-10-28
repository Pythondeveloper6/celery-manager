import { API_BASE as BASE, forbiddenMessage } from "./base";

// The fields an operator edits. `days_of_week` is 0 = Monday ... 6 = Sunday;
// `applies_to` is "*" for every schedule or a list of schedule names.
export interface MaintenanceWindowInput {
  name: string;
  days_of_week: number[];
  start_time: string; // "HH:MM", 24-hour, in `timezone`
  duration_minutes: number;
  timezone: string; // IANA name
  applies_to: "*" | string[];
  enabled: boolean;
}

export interface MaintenanceWindow extends MaintenanceWindowInput {
  id: number;
  created_by: string;
  created_at: string | null;
  updated_by: string | null;
  updated_at: string | null;
  // In progress right now (and enabled).
  active: boolean;
  // The window in progress, or else the next one (ISO 8601 UTC); null never
  // happens for a valid window but is typed so a bad row can't crash the list.
  next_start: string | null;
  next_end: string | null;
}

export interface MaintenanceWindowList {
  // The Celery app's timezone: what a new window starts with.
  default_timezone: string;
  windows: MaintenanceWindow[];
}

export interface WindowSpan {
  start: string;
  end: string;
}

async function detailMessage(response: Response, fallback: string): Promise<string> {
  try {
    const body = await response.json();
    if (typeof body?.detail === "string" && body.detail.trim()) return body.detail.trim();
  } catch {
    // Not JSON; fall through.
  }
  return fallback;
}

async function send<T>(path: string, method: string, body: unknown, fallback: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${BASE}/maintenance-windows${path}`, {
    method,
    credentials: "include",
    signal,
    ...(body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  });
  if (response.status === 403) throw new Error(await forbiddenMessage(response));
  if (!response.ok) throw new Error(await detailMessage(response, fallback));
  return response.json();
}

export async function listMaintenanceWindows(): Promise<MaintenanceWindowList> {
  const response = await fetch(`${BASE}/maintenance-windows`, { credentials: "include" });
  if (!response.ok) throw new Error("Couldn't load maintenance windows.");
  return response.json();
}

export function createMaintenanceWindow(input: MaintenanceWindowInput): Promise<MaintenanceWindow> {
  return send("", "POST", input, "Couldn't save the maintenance window.");
}

export function updateMaintenanceWindow(id: number, input: MaintenanceWindowInput): Promise<MaintenanceWindow> {
  return send(`/${id}`, "PUT", input, "Couldn't save the maintenance window.");
}

export async function deleteMaintenanceWindow(id: number): Promise<void> {
  await send(`/${id}`, "DELETE", undefined, "Couldn't delete the maintenance window.");
}

// The next five spans of a recurrence, without saving it. Any signed-in role.
export async function previewMaintenanceWindow(
  input: Pick<MaintenanceWindowInput, "days_of_week" | "start_time" | "duration_minutes" | "timezone">,
  signal?: AbortSignal,
): Promise<WindowSpan[]> {
  const body = await send<{ windows: WindowSpan[] }>("/preview", "POST", input, "Couldn't preview this window.", signal);
  return body.windows;
}
