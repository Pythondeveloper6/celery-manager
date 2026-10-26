import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { getFailures, type FailureGroup, type RangeKey } from "../api/analytics";
import { bulkRetryTasks } from "../api/tasks";
import { formatWhen } from "../lib/insights";

function taskTypeHref(name: string): string {
  return `/task-types/${encodeURIComponent(name)}`;
}

function runsHref(group: FailureGroup): string {
  const params = new URLSearchParams({ status: "failed" });
  // One affected task: narrow to it. Several: the Failed tab alone is the honest link.
  if (group.task_names.length === 1) params.set("q", group.task_names[0]);
  return `/?${params.toString()}`;
}

function GroupCard({ group, showTasks }: { group: FailureGroup; showTasks: boolean }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const retryCount = group.task_ids.length;

  const retry = async () => {
    setBusy(true);
    try {
      const { new_task_ids } = await bulkRetryTasks(group.task_ids);
      setResult({ ok: true, text: `Queued ${new_task_ids.length} retr${new_task_ids.length === 1 ? "y" : "ies"}.` });
    } catch (err) {
      setResult({ ok: false, text: err instanceof Error ? `${err.message} Some runs may already have been retried; check the audit log.` : "Retry failed." });
    } finally {
      setBusy(false);
      setConfirming(false);
    }
  };

  return (
    <li className="cm-card cm-fail-group">
      <div className="cm-fail-head">
        <span className="cm-fail-count" aria-label={`${group.count} failures`}>
          {group.count.toLocaleString()}
        </span>
        <div className="cm-fail-titles">
          <h3 className="cm-fail-type">{group.exception_type}</h3>
          <p className="cm-fail-message">{group.message || "No message"}</p>
        </div>
      </div>

      <p className="cm-fail-meta">
        {group.frame && (
          <>
            <code>{group.frame}</code>
            <span aria-hidden="true"> · </span>
          </>
        )}
        First seen {formatWhen(group.first_seen)}
        <span aria-hidden="true"> · </span>
        Last seen {formatWhen(group.last_seen)}
      </p>

      {showTasks && (
        <p className="cm-fail-meta">
          Affects{" "}
          {group.task_names.map((name, i) => (
            <span key={name}>
              {i > 0 && ", "}
              <Link to={taskTypeHref(name)} className="cm-link">
                {name}
              </Link>
            </span>
          ))}
        </p>
      )}

      <details className="cm-fail-example">
        <summary>Example exception</summary>
        <pre className="cm-fail-pre">{group.example || "(none recorded)"}</pre>
      </details>

      <div className="cm-fail-actions">
        {group.example_task_ids[0] && (
          <Link to={`/tasks/${group.example_task_ids[0]}`} className="cm-btn cm-btn-outline cm-btn-sm">
            Open latest run
          </Link>
        )}
        <Link to={runsHref(group)} className="cm-btn cm-btn-outline cm-btn-sm">
          Show runs
        </Link>
        {confirming ? (
          <span className="cm-fail-confirm" role="group" aria-label="Confirm retry">
            <span>
              Retry {retryCount.toLocaleString()} run{retryCount === 1 ? "" : "s"}
              {group.task_ids_truncated ? ` (the most recent of ${group.count.toLocaleString()})` : ""}?
            </span>
            <button type="button" className="cm-btn cm-btn-primary cm-btn-sm" onClick={retry} disabled={busy}>
              {busy ? "Retrying…" : `Retry ${retryCount.toLocaleString()}`}
            </button>
            <button type="button" className="cm-btn cm-btn-ghost cm-btn-sm" onClick={() => setConfirming(false)} disabled={busy}>
              Cancel
            </button>
          </span>
        ) : (
          <button type="button" className="cm-btn cm-btn-secondary cm-btn-sm" onClick={() => { setResult(null); setConfirming(true); }}>
            Retry all in group
          </button>
        )}
      </div>
      {result && (
        <p className={result.ok ? "cm-fail-result" : "cm-error"} role={result.ok ? "status" : "alert"}>
          {result.text}
        </p>
      )}
    </li>
  );
}

export function FailureGroups({ range, taskName, groups: preloaded }: { range: RangeKey; taskName?: string; groups?: FailureGroup[] }) {
  const [groups, setGroups] = useState<FailureGroup[] | null>(preloaded ?? null);
  const [truncated, setTruncated] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setError(null);
    getFailures(range, taskName)
      .then((f) => {
        setGroups(f.groups);
        setTruncated(f.truncated);
      })
      .catch((err) => setError(err instanceof Error ? err.message : "Couldn't load the failures."));
  }, [range, taskName]);

  useEffect(() => {
    if (preloaded) return;
    setGroups(null);
    load();
  }, [load, preloaded]);

  const list = preloaded ?? groups;

  if (error) {
    return (
      <div className="cm-stack">
        <p className="cm-error" role="alert">
          {error}
        </p>
        <button type="button" className="cm-btn cm-btn-outline" onClick={load}>
          Try again
        </button>
      </div>
    );
  }
  if (list === null) {
    return (
      <p className="cm-loading">
        <span className="cm-live-dot" aria-hidden="true" />
        Loading failures&hellip;
      </p>
    );
  }
  if (list.length === 0) {
    return <p className="cm-empty-state">No failures in this period. Nothing to group.</p>;
  }
  return (
    <>
      {truncated && <p className="cm-chart-summary">This period has more failures than can be grouped at once; showing the newest.</p>}
      <ul className="cm-fail-list">
        {list.map((g) => (
          <GroupCard key={g.signature} group={g} showTasks={!taskName} />
        ))}
      </ul>
    </>
  );
}
