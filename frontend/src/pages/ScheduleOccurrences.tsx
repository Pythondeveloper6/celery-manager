import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { listOccurrences, pauseOccurrence, type Occurrence, type PauseAction } from "../api/schedules";
import { IconChevronLeft } from "../components/icons";

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
  const { name } = useParams<{ name: string }>();
  const [occurrences, setOccurrences] = useState<Occurrence[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<{ key: string; action: "skip" | "cancel" } | null>(null);
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string | null>(null);
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const refresh = useCallback(async () => {
    if (!name) return;
    try {
      const results = await listOccurrences(name);
      setOccurrences(results);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't load upcoming occurrences.");
    }
  }, [name]);

  useEffect(() => {
    setOccurrences(null);
    setNotice(null);
    setRowErrors({});
    refresh();
  }, [refresh]);

  useEffect(
    () => () => {
      if (noticeTimer.current) clearTimeout(noticeTimer.current);
    },
    [],
  );

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
        </div>
      </header>

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
                  {state === "paused" && o.action && (
                    <span className="cm-occurrence-badge" data-action={o.action}>
                      {PAUSED_LABEL[o.action] ?? o.action}
                    </span>
                  )}
                  {state === "resolved" && <span className="cm-occurrence-badge cm-occurrence-badge-resolved">Already resolved</span>}
                </div>

                {state !== "resolved" && (
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
    </div>
  );
}
