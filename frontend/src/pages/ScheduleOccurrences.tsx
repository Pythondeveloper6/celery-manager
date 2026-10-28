import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { listOccurrences, listSchedules, pauseOccurrence, type Occurrence, type PauseAction, type ScheduleSummary } from "../api/schedules";
import { IconChevronLeft } from "../components/icons";
import { MissedRuns } from "../components/MissedRuns";
import { ScheduleEditor } from "../components/ScheduleEditor";
import { ScheduleToggle } from "../components/ScheduleToggle";
import { formatPausedBy, formatPauseState } from "../lib/pausedBy";
import { useCan } from "../lib/role";

// Keyed by PauseAction rather than a bare string, so adding an action to the
// API without giving it a label here is a compile error instead of a blank
// badge. "reschedule" is gone: the API now 422s it (see api/schedules.ts).
const ACTION_LABEL: Record<PauseAction, string> = {
  skip: "Skip",
  cancel: "Cancel",
};

// How a paused occurrence's state reads beside its time.
const PAUSED_LABEL: Record<PauseAction, string> = {
  skip: "Will be skipped",
  cancel: "Cancelled",
};

function formatTargetTime(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

// An occurrence's `consumed` flag only flips once its scheduled time has
// actually passed (see api/schedules.ts) — every row this view shows is
// still in the future, so "resolved" only fires defensively if the API
// ever returns a stale one. "paused" covers both skip and cancel: either
// way, a human has already decided this occurrence's fate.
function occurrenceState(o: Occurrence): "pending" | "paused" | "resolved" {
  if (o.consumed) return "resolved";
  if (o.action) return "paused";
  return "pending";
}

export function ScheduleOccurrences() {
  const canOperate = useCan("operator");
  const { name } = useParams<{ name: string }>();
  const [occurrences, setOccurrences] = useState<Occurrence[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<{ key: string; action: "skip" | "cancel" } | null>(null);
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string | null>(null);
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const [schedule, setSchedule] = useState<ScheduleSummary | null>(null);
  const [toggleError, setToggleError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);

  const refresh = useCallback(async () => {
    if (!name) return;
    try {
      const [results, all] = await Promise.all([listOccurrences(name), listSchedules()]);
      setOccurrences(results);
      setSchedule(all.find((s) => s.name === name) ?? null);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't load upcoming occurrences.");
    }
  }, [name]);

  useEffect(() => {
    setOccurrences(null);
    setNotice(null);
    setSchedule(null);
    setToggleError(null);
    setEditing(false);
    setRowErrors({});
    refresh();
  }, [refresh]);

  useEffect(
    () => () => {
      if (noticeTimer.current) clearTimeout(noticeTimer.current);
    },
    [],
  );

  function flashNotice(message: string) {
    setNotice(message);
    if (noticeTimer.current) clearTimeout(noticeTimer.current);
    noticeTimer.current = setTimeout(() => setNotice(null), 6000);
  }

  async function handleToggled(updated: ScheduleSummary) {
    setSchedule(updated);
    await refresh();
    flashNotice(updated.paused ? "Schedule paused" : "Schedule resumed");
  }

  async function handleEdited(updated: ScheduleSummary, message: string) {
    setEditing(false);
    setSchedule(updated);
    await refresh();
    flashNotice(message);
  }

  async function handlePause(o: Occurrence, action: "skip" | "cancel") {
    if (!name) return;
    const key = o.target_time;
    setPending({ key, action });
    setRowErrors((prev) => {
      if (!(key in prev)) return prev;
      const next = { ...prev };
      delete next[key];
      return next;
    });
    try {
      await pauseOccurrence(name, o.target_time, action);
      await refresh();
      setNotice(`${ACTION_LABEL[action]} recorded for the run at ${formatTargetTime(o.target_time)}.`);
      if (noticeTimer.current) clearTimeout(noticeTimer.current);
      noticeTimer.current = setTimeout(() => setNotice(null), 6000);
    } catch (err) {
      setRowErrors((prev) => ({
        ...prev,
        [key]: err instanceof Error ? err.message : "Couldn't update this occurrence. Try again.",
      }));
    } finally {
      setPending(null);
    }
  }

  return (
    <div className="cm-page">
      {/* Back to the *schedules* list, not to Tasks. This screen predates
          ScheduleList, so its back link went to the only list that existed
          at the time; now that /schedules is where a person actually arrives
          from, sending them to the task table instead is a dead end that
          costs a second navigation every time. */}
      <Link to="/schedules" className="cm-btn cm-btn-ghost cm-btn-sm cm-back-link">
        <IconChevronLeft />
        All schedules
      </Link>

      <header className="cm-page-header">
        <div className="cm-page-header-titles">
          <h1 className="cm-page-title">{name}</h1>
          <p className="cm-page-lede">
            Upcoming runs. Skipping or cancelling one leaves the schedule itself unchanged.
          </p>
          {schedule && (
            <p className="cm-schedule-edited">
              Runs <code>{schedule.schedule_spec}</code>
              {schedule.schedule_override && (
                <>
                  {" "}
                  &middot; edited {formatPausedBy(schedule.edited_by, schedule.edited_at)}
                  {!schedule.schedule_editing && " (not applied: schedule editing is off)"}
                </>
              )}
            </p>
          )}
        </div>
        {schedule && (
          <div className="cm-header-actions">
            {canOperate && (
              <button
                type="button"
                className="cm-btn cm-btn-outline"
                onClick={() => setEditing(true)}
                title={schedule.schedule_editing ? undefined : "Schedule editing is off. Open to preview or revert."}
              >
                Edit schedule
              </button>
            )}
            {!schedule.paused && (
              <ScheduleToggle name={schedule.name} paused={false} size="md" onChange={handleToggled} onError={setToggleError} />
            )}
          </div>
        )}
      </header>

      {editing && schedule && (
        <ScheduleEditor schedule={schedule} onClose={() => setEditing(false)} onSaved={handleEdited} />
      )}

      {schedule?.paused && (
        <div className="cm-alert" role="status">
          <div className="cm-stack">
            <p>
              <strong>{formatPauseState(schedule.paused_until)}.</strong>{" "}
              {schedule.paused_until
                ? "Firings before then are skipped; after that it resumes by itself."
                : "Every firing is skipped until you resume it."}
              {formatPausedBy(schedule.paused_by, schedule.paused_at) && (
                <> Paused {formatPausedBy(schedule.paused_by, schedule.paused_at)}.</>
              )}
            </p>
            <div>
              <ScheduleToggle name={schedule.name} paused size="md" onChange={handleToggled} onError={setToggleError} />
            </div>
          </div>
        </div>
      )}

      {toggleError && (
        <p className="cm-error" role="alert">
          {toggleError}
        </p>
      )}

      {notice && (
        <p className="cm-notice" role="status">
          {notice}
        </p>
      )}

      {error ? (
        // Same error treatment as the other list screens: the server's own
        // message plus a retry, rather than a sentence with no way forward.
        <div className="cm-stack">
          <p className="cm-error" role="alert">
            {error}
          </p>
          <button type="button" className="cm-btn cm-btn-outline" onClick={refresh}>
            Try again
          </button>
        </div>
      ) : occurrences === null ? (
        <p className="cm-loading">
          <span className="cm-live-dot" aria-hidden="true" />
          Loading upcoming runs&hellip;
        </p>
      ) : occurrences.length === 0 ? (
        <div className="cm-card">
          <p className="cm-empty-state">No upcoming runs to show yet. Check that the schedule is enabled.</p>
        </div>
      ) : (
        <ul className="cm-card cm-card-flush cm-occurrence-list">
          {occurrences.map((o) => {
            const key = o.target_time;
            const state = occurrenceState(o);
            const busy = pending?.key === key;
            const rowError = rowErrors[key];

            return (
              <li key={key} className="cm-occurrence-row" data-state={state} data-action={o.action ?? undefined}>
                <div className="cm-occurrence-main">
                  <span className="cm-occurrence-time">{formatTargetTime(o.target_time)}</span>
                  {o.paused && state !== "resolved" && !o.action && (
                    <span className="cm-occurrence-badge" data-action="skip">
                      Will be skipped
                    </span>
                  )}
                  {o.maintenance_window && state !== "resolved" && (
                    <span className="cm-occurrence-badge" data-action="skip" title="Skipped automatically by a maintenance window">
                      Maintenance window: {o.maintenance_window}
                    </span>
                  )}
                  {state === "paused" && o.action && (
                    <span className="cm-occurrence-badge" data-action={o.action}>
                      {PAUSED_LABEL[o.action] ?? o.action}
                    </span>
                  )}
                  {state === "resolved" && <span className="cm-occurrence-badge cm-occurrence-badge-resolved">Already resolved</span>}
                </div>

                {canOperate && state !== "resolved" && (
                  <div className="cm-occurrence-actions">
                    <button
                      type="button"
                      className="cm-btn cm-btn-outline cm-btn-sm"
                      aria-pressed={o.action === "skip"}
                      disabled={busy}
                      onClick={() => handlePause(o, "skip")}
                    >
                      {busy && pending?.action === "skip" ? "Skipping…" : o.action === "skip" ? "Skipped" : "Skip this run"}
                    </button>
                    <button
                      type="button"
                      className="cm-btn cm-btn-outline cm-btn-sm"
                      aria-pressed={o.action === "cancel"}
                      disabled={busy}
                      onClick={() => handlePause(o, "cancel")}
                    >
                      {busy && pending?.action === "cancel" ? "Cancelling…" : o.action === "cancel" ? "Cancelled" : "Cancel this run"}
                    </button>
                  </div>
                )}

                {rowError && (
                  <p className="cm-error cm-occurrence-row-error" role="alert">
                    {rowError}
                  </p>
                )}
              </li>
            );
          })}
        </ul>
      )}

      {name && <MissedRuns name={name} />}
    </div>
  );
}
