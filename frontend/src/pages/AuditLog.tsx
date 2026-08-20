import { useCallback, useEffect, useState } from "react";
import { listAuditLog, type AuditEntry } from "../api/audit";
import { BrandMark, useBranding } from "../branding";

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
  const { settings } = useBranding();
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
          <p className="cm-page-eyebrow">
            <BrandMark />
            {settings.brand_name}
          </p>
          <h1 className="cm-page-title">Audit log</h1>
        </div>
      </header>

      <div className="cm-panel">
        {error ? (
          <div className="cm-panel-stack">
            <p className="cm-error" role="alert">
              {error}
            </p>
            <button type="button" className="cm-btn cm-btn-outline" onClick={refresh}>
              Try again
            </button>
          </div>
        ) : entries === null ? (
          <p className="cm-loading">
            <span className="cm-live-dot" aria-hidden="true" />
            Loading audit log&hellip;
          </p>
        ) : entries.length === 0 ? (
          <p className="cm-empty-state">No manual actions recorded yet.</p>
        ) : (
          <div className="cm-table-scroll">
            <table className="cm-audit-log">
              <thead>
                <tr>
                  <th scope="col">When</th>
                  <th scope="col">User</th>
                  <th scope="col">Action</th>
                  <th scope="col">Target</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((e, i) => (
                  <tr key={i}>
                    <td className="cm-timestamp">{formatTimestamp(e.created_at)}</td>
                    <td className="cm-audit-user">{e.username}</td>
                    <td>
                      <span className="cm-audit-action">{e.action}</span>
                    </td>
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
