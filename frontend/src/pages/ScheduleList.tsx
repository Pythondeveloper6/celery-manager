import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { listSchedules, type ScheduleSummary } from "../api/schedules";
import { BrandMark, useBranding } from "../branding";

// This is the missing discovery step for ScheduleOccurrences (Task 14):
// that view is keyed by an exact schedule name with no way to reach it
// except a hand-typed URL. Structurally this mirrors TaskList (fetch on
// mount, cm-panel loading/error/empty states, a Link-wrapped row leading
// into the detail screen) since both are "browse then drill into one
// item" screens — but each row renders as its own card, matching the
// occurrence-list visual family (Task 14) rather than TaskList's table,
// since a schedule row carries no per-column data dense enough to need
// tabular alignment (just a name, a task, and a spec).
export function ScheduleList() {
  const { settings } = useBranding();
  const [schedules, setSchedules] = useState<ScheduleSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const results = await listSchedules();
      setSchedules(results);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't load schedules.");
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  return (
    <div className="cm-page">
      <header className="cm-page-header">
        <div className="cm-page-header-titles">
          <p className="cm-page-eyebrow">
            <BrandMark />
            {settings.brand_name}
          </p>
          <h1 className="cm-page-title">Periodic schedules</h1>
        </div>
      </header>

      <div className="cm-panel">
        {error ? (
          // Same error treatment as TaskList and the audit log: the server's
          // message, and a button that retries it. An error screen with
          // nothing to click is a dead end.
          <div className="cm-panel-stack">
            <p className="cm-error" role="alert">
              {error}
            </p>
            <button type="button" className="cm-btn cm-btn-outline" onClick={refresh}>
              Try again
            </button>
          </div>
        ) : schedules === null ? (
          <p className="cm-loading">
            <span className="cm-live-dot" aria-hidden="true" />
            Loading schedules&hellip;
          </p>
        ) : schedules.length === 0 ? (
          <p className="cm-empty-state">No periodic schedules configured yet.</p>
        ) : (
          <ul className="cm-schedule-list">
            {schedules.map((s) => (
              <li key={s.name} className="cm-schedule-list-row">
                <Link to={`/schedules/${encodeURIComponent(s.name)}`} className="cm-schedule-list-link">
                  <span className="cm-schedule-list-name">{s.name}</span>
                  <span className="cm-schedule-list-meta">
                    {s.task_name} &middot; {s.schedule_spec}
                  </span>
                </Link>
                {!s.enabled && <span className="cm-badge">disabled</span>}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
