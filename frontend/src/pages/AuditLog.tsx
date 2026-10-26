import { useCallback, useEffect, useState } from "react";
import { listAuditLog, type AuditEntry } from "../api/audit";

function formatTimestamp(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function AuditLog() {
  const [entries, setEntries] = useState<AuditEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Same shape as ScheduleList and TaskList: the server's own words, and a
  // way to try again. This used to swallow the error and substitute a fixed
  // sentence, so "your session expired" and "the database is down" arrived
  // as the same unactionable message with nothing to click.
  const refresh = useCallback(() => {
    setError(null);
    listAuditLog()
      .then(setEntries)
      .catch((err) => setError(err instanceof Error ? err.message : "Couldn't load the audit log."));
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  return (
    <div className="cm-page">
      <header className="cm-page-header">
        <div className="cm-page-header-titles">
          <h1 className="cm-page-title">Audit log</h1>
          <p className="cm-page-lede">Every manual action taken from this dashboard, newest first.</p>
        </div>
      </header>

      <div className="cm-card cm-card-flush">
        {error ? (
          <div className="cm-card-pad cm-stack">
            <p className="cm-error" role="alert">
              {error}
            </p>
            <button type="button" className="cm-btn cm-btn-outline" onClick={refresh}>
              Try again
            </button>
          </div>
        ) : entries === null ? (
          <div className="cm-card-pad">
            <p className="cm-loading">
              <span className="cm-live-dot" aria-hidden="true" />
              Loading audit log&hellip;
            </p>
          </div>
        ) : entries.length === 0 ? (
          <p className="cm-empty-state">
            No manual actions recorded yet. Retries, cancels and schedule changes made here will be listed.
          </p>
        ) : (
          <div className="cm-table-scroll">
            <table className="cm-table cm-table-stack">
              <thead>
                <tr>
                  <th scope="col" className="cm-col-fit">
                    When
                  </th>
                  <th scope="col" className="cm-col-fit">
                    User
                  </th>
                  <th scope="col" className="cm-col-fit">
                    Action
                  </th>
                  <th scope="col">Target</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((e, i) => (
                  <tr key={i}>
                    <td className="cm-col-fit cm-timestamp">{formatTimestamp(e.created_at)}</td>
                    <td className="cm-col-fit cm-audit-user">{e.username}</td>
                    <td className="cm-col-fit cm-cell-primary cm-audit-action">{e.action}</td>
                    <td className="cm-audit-target">
                      {e.target_type}:{e.target_id}
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
