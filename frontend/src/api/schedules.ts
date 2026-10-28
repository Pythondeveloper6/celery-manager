import { API_BASE as BASE, forbiddenMessage } from "./base";

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
  // True when the whole schedule is paused (`pauseSchedule`) AND this
  // occurrence falls before the pause ends: it will be recorded as skipped
  // when beat fires it, whatever `action` says. Per occurrence: for a pause
  // with a `paused_until`, occurrences at or after that time are false (they
  // run normally); an indefinite pause makes every occurrence true.
  // `action` keeps reporting any per-occurrence override independently, so
  // resuming shows the per-occurrence skips that are still in place.
  paused: boolean;
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
  if (response.status === 403) throw new Error(await forbiddenMessage(response));
  if (!response.ok) throw new Error("Couldn't update this occurrence.");
}

export interface ScheduleSummary {
  name: string;
  task_name: string;
  schedule_spec: string;
  // Always true today: `sync_periodic_schedules` sets it for every entry in
  // the host app's beat_schedule. Not the pause state — see `paused`.
  enabled: boolean;
  // Whole-schedule pause (`pauseSchedule` / `resumeSchedule`). While true,
  // every firing that reaches a `@track`ed task is recorded as skipped and
  // the body doesn't run. `paused_at` (ISO 8601, UTC) and `paused_by`
  // (username) describe when the current pause began and who started it;
  // both are null when `paused` is false.
  paused: boolean;
  paused_at: string | null;
  paused_by: string | null;
  // ISO 8601 UTC end of a timed pause (auto-resume); null for an indefinite
  // pause or when not paused. A pause whose end has passed is reported as
  // not paused.
  paused_until: string | null;
  // Scheduled runs from the last 24 hours that were due but never arrived
  // (see `getMissedRuns`); 0 when none or when detection isn't active yet.
  missed_count: number;
}

export async function listSchedules(): Promise<ScheduleSummary[]> {
  const response = await fetch(`${BASE}/schedules`, { credentials: "include" });
  if (!response.ok) throw new Error("Couldn't load schedules.");
  return response.json();
}

// Pause every firing of a schedule until resumed, or until `until` (then it
// resumes by itself). Repeating the pause keeps the original
// paused_at/paused_by and only moves the end time. Resolves to the
// schedule's updated summary.
export async function pauseSchedule(name: string, until?: Date): Promise<ScheduleSummary> {
  const response = await fetch(`${BASE}/schedules/${encodeURIComponent(name)}/pause`, {
    method: "POST",
    credentials: "include",
    ...(until
      ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify({ until: until.toISOString() }) }
      : {}),
  });
  if (response.status === 403) throw new Error(await forbiddenMessage(response));
  if (response.status === 422) throw new Error("Choose a time in the future.");
  if (!response.ok) throw new Error("Couldn't pause this schedule.");
  return response.json();
}

// Resume a paused schedule. Nothing runs immediately and firings missed while
// paused are not caught up: the next regular beat firing simply runs.
// Idempotent.
export async function resumeSchedule(name: string): Promise<ScheduleSummary> {
  const response = await fetch(`${BASE}/schedules/${encodeURIComponent(name)}/resume`, {
    method: "POST",
    credentials: "include",
  });
  if (response.status === 403) throw new Error(await forbiddenMessage(response));
  if (!response.ok) throw new Error("Couldn't resume this schedule.");
  return response.json();
}

export interface MissedRuns {
  // False until beat has stamped a run of this schedule (run beat with
  // `-S celery_manager.beat:Scheduler`): before that a missing run cannot be
  // told from an unstamped one, so nothing is reported.
  active: boolean;
  // ISO 8601 UTC of the first stamped run, where judging starts; null when inactive.
  tracking_since: string | null;
  // How long after its due time a run may still arrive before it counts as missed.
  grace_seconds: number;
  // Runs due in the last 7 days that never arrived, oldest first.
  missed: { due_at: string }[];
}

export async function getMissedRuns(name: string): Promise<MissedRuns> {
  const response = await fetch(`${BASE}/schedules/${encodeURIComponent(name)}/missed`, { credentials: "include" });
  if (!response.ok) throw new Error("Couldn't load missed runs.");
  return response.json();
}
