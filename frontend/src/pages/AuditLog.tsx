import { useEffect, useState } from "react";
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

  useEffect(() => {
    listAuditLog()
      .then(setEntries)
      .catch(() => setError("Couldn't load the audit log."));
  }, []);

  return (
    <div className="cm-page">
      <header className="cm-page-header">
        <div className="cm-page-header-titles">
          <p className="cm-page-eyebrow">
            <span className="cm-brand-mark" aria-hidden="true" />
            celery-manager
          </p>
          <h1 className="cm-page-title">Audit log</h1>
        </div>
      </header>

      <div className="cm-panel">
        {error ? (
          <p className="cm-error" role="alert">
            {error}
          </p>
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
