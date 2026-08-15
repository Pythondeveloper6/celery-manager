import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { listTasks, subscribeToUpdates, type TaskSummary } from "../api/tasks";

const STATUS_FILTERS = ["all", "queued", "started", "succeeded", "failed", "retried", "skipped", "revoked"];

function formatTimestamp(value: string | null): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export function TaskList() {
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [status, setStatus] = useState("all");
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Kept in a ref (rather than a `refresh` dependency on `status`) so the
  // SSE subscription below can stay open across filter changes instead of
  // tearing down and reconnecting `EventSource` on every click.
  const statusRef = useRef(status);
  useEffect(() => {
    statusRef.current = status;
  }, [status]);

  // Only the very first fetch shows the full "Loading tasks…" takeover;
  // every fetch after that (filter switch or a live SSE event) just dims
  // the panel briefly via `refreshing` so the existing rows never flicker
  // away while fresher data is on the way.
  const hasLoadedRef = useRef(false);

  const refresh = useCallback(async () => {
    if (hasLoadedRef.current) setRefreshing(true);
    try {
      const current = statusRef.current;
      const results = await listTasks(current === "all" ? {} : { status: current });
      setTasks(results);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't load tasks.");
    } finally {
      hasLoadedRef.current = true;
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [status, refresh]);

  useEffect(() => subscribeToUpdates(() => refresh()), [refresh]);

  return (
    <div className="cm-page">
      <header className="cm-page-header">
        <div className="cm-page-header-titles">
          <p className="cm-page-eyebrow">
            <span className="cm-brand-mark" aria-hidden="true" />
            celery-manager
          </p>
          <h1 className="cm-page-title">Task runs</h1>
        </div>
        <div className="cm-live-indicator" title="Updates as workers report task events">
          <span className="cm-live-dot" aria-hidden="true" />
          Live
        </div>
      </header>

      <div className="cm-filter-bar" role="group" aria-label="Filter by status">
        {STATUS_FILTERS.map((s) => (
          <button key={s} type="button" aria-pressed={s === status} onClick={() => setStatus(s)}>
            {s}
          </button>
        ))}
      </div>

      <div className={`cm-panel${refreshing ? " cm-panel-refreshing" : ""}`}>
        {loading ? (
          <p className="cm-loading">
            <span className="cm-live-dot" aria-hidden="true" />
            Loading tasks&hellip;
          </p>
        ) : error ? (
          <p className="cm-error" role="alert">
            {error}
          </p>
        ) : tasks.length === 0 ? (
          <p className="cm-empty-state">
            {status === "all" ? "No tasks recorded yet." : `No ${status} tasks match this filter yet.`}
          </p>
        ) : (
          <div className="cm-table-scroll">
            <table className="cm-task-table">
              <thead>
                <tr>
                  <th scope="col">Task</th>
                  <th scope="col">Status</th>
                  <th scope="col">Started</th>
                </tr>
              </thead>
              <tbody>
                {tasks.map((t) => (
                  <tr key={t.task_id}>
                    <td>
                      <Link to={`/tasks/${t.task_id}`} className="cm-task-link">
                        <span className="cm-task-name">{t.task_name}</span>
                        <span className="cm-task-id">{t.task_id}</span>
                      </Link>
                    </td>
                    <td>
                      <span className="cm-status-badge" data-status={t.status}>
                        {t.status}
                      </span>
                    </td>
                    <td className="cm-timestamp">{formatTimestamp(t.started_at)}</td>
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
