import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { listSchedules, type ScheduleSummary } from "../api/schedules";
import { ScheduleToggle } from "../components/ScheduleToggle";
import { formatPausedBy } from "../lib/pausedBy";

// The discovery step for ScheduleOccurrences, which is keyed by an exact
// schedule name. Same shape as the other list screens — fetch on mount,
// loading/error/empty states, and the shared table style — with each name
// linking into that schedule's upcoming runs.
export function ScheduleList() {
  const [schedules, setSchedules] = useState<ScheduleSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string | null>(null);
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(
    () => () => {
      if (noticeTimer.current) clearTimeout(noticeTimer.current);
    },
    [],
  );

  function handleChange(updated: ScheduleSummary) {
    setSchedules((prev) => prev && prev.map((s) => (s.name === updated.name ? updated : s)));
    setNotice(updated.paused ? "Schedule paused" : "Schedule resumed");
    if (noticeTimer.current) clearTimeout(noticeTimer.current);
    noticeTimer.current = setTimeout(() => setNotice(null), 6000);
  }

  function handleRowError(name: string, message: string | null) {
    setRowErrors((prev) => {
      const next = { ...prev };
      if (message) next[name] = message;
      else delete next[name];
      return next;
    });
  }

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
          <h1 className="cm-page-title">Schedules</h1>
          <p className="cm-page-lede">Periodic tasks. Open one to skip or cancel an upcoming run.</p>
        </div>
      </header>

      {notice && (
        <p className="cm-notice" role="status">
          {notice}
        </p>
      )}

      <div className="cm-card cm-card-flush">
        {error ? (
          // Same error treatment as TaskList and the audit log: the server's
          // message, and a button that retries it. An error screen with
          // nothing to click is a dead end.
          <div className="cm-card-pad cm-stack">
            <p className="cm-error" role="alert">
              {error}
            </p>
            <button type="button" className="cm-btn cm-btn-outline" onClick={refresh}>
              Try again
            </button>
          </div>
        ) : schedules === null ? (
          <div className="cm-card-pad">
            <p className="cm-loading">
              <span className="cm-live-dot" aria-hidden="true" />
              Loading schedules&hellip;
            </p>
          </div>
        ) : schedules.length === 0 ? (
          <p className="cm-empty-state">
            No periodic schedules configured yet. Add one to your Celery beat schedule and it appears here.
          </p>
        ) : (
          <div className="cm-table-scroll">
            <table className="cm-table cm-table-stack cm-schedules-table">
              <thead>
                <tr>
                  <th scope="col">Schedule</th>
                  <th scope="col">Task</th>
                  <th scope="col">Runs</th>
                  <th scope="col" className="cm-col-fit">
                    State
                  </th>
                  <th scope="col" className="cm-col-fit">
                    <span className="cm-visually-hidden">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {schedules.map((s) => (
                  <tr key={s.name}>
                    <td className="cm-cell-primary">
                      <Link to={`/schedules/${encodeURIComponent(s.name)}`} className="cm-schedule-link">
                        {s.name}
                      </Link>
                    </td>
                    <td>{s.task_name}</td>
                    <td>{s.schedule_spec}</td>
                    <td className="cm-col-fit">
                      {s.paused ? (
                        <span className="cm-badge" title={formatPausedBy(s.paused_by, s.paused_at)}>
                          Paused
                          {s.paused_by && <span className="cm-schedule-enabled"> {formatPausedBy(s.paused_by, s.paused_at)}</span>}
                        </span>
                      ) : s.enabled ? (
                        <span className="cm-schedule-enabled">Enabled</span>
                      ) : (
                        <span className="cm-badge">Disabled</span>
                      )}
                    </td>
                    <td className="cm-col-fit">
                      <div className="cm-stack">
                        <ScheduleToggle
                          name={s.name}
                          paused={s.paused}
                          onChange={handleChange}
                          onError={(m) => handleRowError(s.name, m)}
                        />
                        {rowErrors[s.name] && (
                          <p className="cm-error" role="alert">
                            {rowErrors[s.name]}
                          </p>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
