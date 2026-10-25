import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { listSchedules, type ScheduleSummary } from "../api/schedules";

// The discovery step for ScheduleOccurrences, which is keyed by an exact
// schedule name. Same shape as the other list screens — fetch on mount,
// loading/error/empty states, and the shared table style — with each name
// linking into that schedule's upcoming runs.
export function ScheduleList() {
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
          <h1 className="cm-page-title">Schedules</h1>
          <p className="cm-page-lede">Periodic tasks. Open one to skip or cancel an upcoming run.</p>
        </div>
      </header>

      <div className="cm-panel cm-panel-flush">
        {error ? (
          // Same error treatment as TaskList and the audit log: the server's
          // message, and a button that retries it. An error screen with
          // nothing to click is a dead end.
          <div className="cm-panel-pad cm-panel-stack">
            <p className="cm-error" role="alert">
              {error}
            </p>
            <button type="button" className="cm-btn cm-btn-outline" onClick={refresh}>
              Try again
            </button>
          </div>
        ) : schedules === null ? (
          <div className="cm-panel-pad">
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
                      {s.enabled ? (
                        <span className="cm-schedule-enabled">Enabled</span>
                      ) : (
                        <span className="cm-badge">Disabled</span>
                      )}
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
