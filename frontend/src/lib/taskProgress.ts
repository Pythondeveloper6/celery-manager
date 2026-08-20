import { useEffect, useState } from "react";
import type { TaskSummary } from "../api/tasks";

// Shared derivations for "how far along is this, and how long has it taken".
// Deliberately one module used by both the task list and the task detail
// screen: a run showing "42%" in the list and "43%" on its own page — or two
// slightly different clocks — reads as a bug even when both are individually
// defensible. The server ships raw numbers only (see api/routes/tasks.py) and
// everything derived lives here.

/** Statuses that mean "this run is happening right now", i.e. its elapsed
 *  time is still growing and its progress can still change. */
export function isRunning(task: Pick<TaskSummary, "status">): boolean {
  return task.status === "started";
}

/** Parse an API timestamp to epoch millis, or null.
 *
 *  The API stamps an explicit UTC offset on every datetime it emits, so
 *  `new Date` is unambiguous here. Anything unparseable becomes null rather
 *  than NaN, so a single bad value degrades to "unknown" instead of
 *  poisoning every arithmetic result downstream with NaN. */
export function toMillis(value: string | null): number | null {
  if (!value) return null;
  const parsed = new Date(value).getTime();
  return Number.isNaN(parsed) ? null : parsed;
}

/** Completion fraction in [0, 1], or null when it can't be known.
 *
 *  null covers three genuinely different cases the UI must not flatten into
 *  "0%": the task never reported progress, it reported open-ended progress
 *  with no denominator ("412 rows processed", total unknown), or the total is
 *  nonsense. A bar sitting at 0% looks like a stuck task; no bar at all
 *  correctly says "this task isn't instrumented for progress". */
export function progressFraction(task: TaskSummary): number | null {
  const { progress_current: current, progress_total: total } = task;
  if (current === null || total === null || total <= 0) return null;
  // Clamped because a task is free to report current > total (an off-by-one
  // in someone else's loop must not render a bar overflowing its track).
  return Math.min(1, Math.max(0, current / total));
}

/** The completion percentage to *show*, 0-100, or null when there is none.
 *
 *  Split out of the two screens rather than left as one `Math.round` each,
 *  because they had already drifted: the detail page deliberately never
 *  rounds a still-running task up to 100 ("rounding 0.998 up reads as a
 *  finished task that is somehow still working") while the list rounded
 *  plainly — so a run at 99.6% showed 100% in the table and 99% on its own
 *  page. That is precisely the disagreement this module exists to prevent,
 *  and it is now one function with one rule: a run that is still running
 *  stops at 99 until it is genuinely complete. */
export function progressPercent(task: TaskSummary): number | null {
  const fraction = progressFraction(task);
  if (fraction === null) return null;
  if (fraction >= 1) return 100;
  const rounded = Math.round(fraction * 100);
  return isRunning(task) ? Math.min(99, rounded) : rounded;
}

/** Seconds this run has been going, live for a running task and final for a
 *  finished one.
 *
 *  Prefers the server's own `runtime_seconds` for finished runs — that is the
 *  worker's measurement of the task body, which is the honest answer, and it
 *  stays stable no matter how skewed the viewer's clock is. Falls back to the
 *  timestamp difference when runtime wasn't recorded (e.g. a run the events
 *  listener saw but the decorator never wrapped). */
export function elapsedSeconds(task: TaskSummary, nowMillis: number): number | null {
  if (!isRunning(task) && task.runtime_seconds !== null) return task.runtime_seconds;

  const started = toMillis(task.started_at);
  if (started === null) return null;

  const end = isRunning(task) ? nowMillis : toMillis(task.finished_at) ?? nowMillis;
  // Never negative: a viewer's clock running behind the worker's would
  // otherwise produce "-3s elapsed", which reads as broken software rather
  // than as the clock skew it actually is.
  return Math.max(0, (end - started) / 1000);
}

/** Projected seconds remaining, from elapsed time and completion fraction.
 *
 *  Straight-line extrapolation: if 40% took 20s, the remaining 60% takes 30s.
 *  That assumption is wrong for work with uneven per-unit cost, which is why
 *  callers must present the result as an estimate and why it returns null
 *  rather than a wild number in the cases where it would be meaningless —
 *  no progress, nothing done yet (no rate to extrapolate from), or already
 *  complete. */
export function projectedRemainingSeconds(task: TaskSummary, elapsed: number | null): number | null {
  if (!isRunning(task)) return null;
  const fraction = progressFraction(task);
  if (fraction === null || fraction <= 0 || fraction >= 1) return null;
  if (elapsed === null || elapsed <= 0) return null;
  return (elapsed / fraction) * (1 - fraction);
}

/** Compact human duration: "0.4s", "42s", "3m 07s", "2h 13m".
 *
 *  Sub-minute durations keep a decimal because many Celery tasks finish in
 *  well under a second and "0s" would erase the difference between a 30ms
 *  task and a 900ms one. Above a minute the decimal is noise, so it goes. */
export function formatDuration(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds) || seconds < 0) return "—";
  if (seconds < 1) return `${seconds.toFixed(2)}s`;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;

  const whole = Math.floor(seconds);
  const minutes = Math.floor(whole / 60);
  if (minutes < 60) return `${minutes}m ${String(whole % 60).padStart(2, "0")}s`;

  const hours = Math.floor(minutes / 60);
  return `${hours}h ${String(minutes % 60).padStart(2, "0")}m`;
}

/** Seconds since an API timestamp, or null. Used to decide whether a running
 *  task's progress has gone quiet. */
export function secondsSince(value: string | null, nowMillis: number): number | null {
  const at = toMillis(value);
  if (at === null) return null;
  return Math.max(0, (nowMillis - at) / 1000);
}

/**
 * A clock that ticks only while something on screen actually needs it.
 *
 * `active` is the whole point: a list with no running tasks, or a detail page
 * for a finished run, must not re-render once a second forever. Callers pass
 * `active = <any task is running>` so an idle dashboard settles completely —
 * which also keeps a backgrounded tab from burning battery redrawing static
 * timestamps.
 */
export function useNow(active: boolean, intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!active) return;
    // Re-sync immediately on activation: a task that started running while
    // the clock was stopped would otherwise show a stale first frame.
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(timer);
  }, [active, intervalMs]);

  return now;
}
