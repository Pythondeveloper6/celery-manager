import { API_BASE as BASE } from "./base";

// The only actions POST /api/schedules/{name}/occurrences/pause accepts.
// Kept as one named type so the request body and the response shape can't
// drift apart from each other or from the backend's Literal["skip","cancel"].
export type PauseAction = "skip" | "cancel";

export interface Occurrence {
  target_time: string;
  // null until a user (or a prior request) has paused this occurrence —
  // see `list_upcoming_with_overrides` in `control/schedules.py`. The
  // brief's own snippet typed this as a bare `string`, but the real
  // endpoint (Task 10, confirmed by `test_api_schedules.py`) returns
  // `null` for every occurrence nobody has acted on yet, which is most
  // of them on a fresh schedule.
  // "reschedule" is a value the ScheduleOverride column still permits, but
  // the pause endpoint now rejects it with a 422 (nothing downstream ever
  // implemented it — the decorator only acts on skip/cancel, so a stored
  // "reschedule" did nothing while showing here as paused). Narrowed to
  // match, so the UI can't construct a request the backend would reject.
  action: PauseAction | null;
  // Only flips to true once the task actually runs (or is skipped) at
  // its scheduled time — `list_upcoming_with_overrides` never marks an
  // override consumed itself. A *future* occurrence with a non-null
  // `action` is "paused, not yet due"; `consumed` is a separate, later
  // state ("this occurrence has already passed").
  consumed: boolean;
}

export async function listOccurrences(name: string): Promise<Occurrence[]> {
  const response = await fetch(`${BASE}/schedules/${encodeURIComponent(name)}/occurrences`, { credentials: "include" });
  if (!response.ok) throw new Error("Couldn't load upcoming occurrences.");
  return response.json();
}

export async function pauseOccurrence(name: string, targetTime: string, action: PauseAction): Promise<void> {
  const response = await fetch(`${BASE}/schedules/${encodeURIComponent(name)}/occurrences/pause`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ target_time: targetTime, action }),
  });
  if (!response.ok) throw new Error("Couldn't update this occurrence.");
}

export interface ScheduleSummary {
  name: string;
  task_name: string;
  schedule_spec: string;
  enabled: boolean;
}

export async function listSchedules(): Promise<ScheduleSummary[]> {
  const response = await fetch(`${BASE}/schedules`, { credentials: "include" });
  if (!response.ok) throw new Error("Couldn't load schedules.");
  return response.json();
}
