import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { bulkRetryTasks, listTasks, subscribeToUpdates, type TaskSummary } from "../api/tasks";

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
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkRetrying, setBulkRetrying] = useState(false);
  const [bulkError, setBulkError] = useState<string | null>(null);
  const [bulkNotice, setBulkNotice] = useState<string | null>(null);

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

  // `silent` skips the `refreshing` dim. Background polling (below) uses it
  // so the panel doesn't pulse every few seconds for a fetch the user never
  // asked for; user-initiated and SSE-driven refreshes still show the dim.
  const refresh = useCallback(async ({ silent = false }: { silent?: boolean } = {}) => {
    if (hasLoadedRef.current && !silent) setRefreshing(true);
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

  // Polling fallback, deliberately *alongside* the SSE subscription above
  // rather than instead of it. The EventBus that /api/stream reads from lives
  // in the API process's memory, but the documented deployment runs the
  // events listener as a separate OS process — which cannot publish into it.
  // So in a real deployment SSE connects and then delivers nothing, and this
  // poll is what actually keeps the list live. When SSE *does* deliver
  // (single-process setups, and once the backlog's cross-process fix lands),
  // both paths simply call the same refresh and the poll is redundant rather
  // than harmful. See plan.md's Backlog, "Cross-process SSE delivery".
  useEffect(() => {
    const interval = setInterval(() => refresh({ silent: true }), 5000);
    return () => clearInterval(interval);
  }, [refresh]);

  // Switching filters clears the selection outright (simplest mental
  // model: a selection belongs to the list you made it in) and also
  // clears any leftover bulk-action feedback from the previous view.
  useEffect(() => {
    setSelected(new Set());
    setBulkError(null);
    setBulkNotice(null);
  }, [status]);

  // A stale selection pointing at a row the list no longer shows is worse
  // than an empty one — a bulk retry must never silently act on a
  // task_id the user can no longer see. The effect above only fires on a
  // filter *switch*; it does nothing when the SAME filter's `tasks` array
  // changes shape underneath a live selection — e.g. a background SSE
  // event (`subscribeToUpdates` -> `refresh()`, line ~64) moves a
  // selected task out of "failed" without `status` itself changing. So
  // prune `selected` against the current `tasks` id set on every `tasks`
  // change too, not just on filter change — this is what actually
  // guarantees "selected never contains an id that isn't currently
  // visible," which the filter-only reset does not.
  useEffect(() => {
    const visibleIds = new Set(tasks.map((t) => t.task_id));
    setSelected((prev) => {
      let changed = false;
      const next = new Set<string>();
      for (const id of prev) {
        if (visibleIds.has(id)) {
          next.add(id);
        } else {
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [tasks]);

  function toggle(taskId: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(taskId)) {
        next.delete(taskId);
      } else {
        next.add(taskId);
      }
      return next;
    });
  }

  async function handleBulkRetry() {
    const taskIds = [...selected];
    if (taskIds.length === 0) return;
    setBulkRetrying(true);
    setBulkError(null);
    setBulkNotice(null);
    try {
      const { new_task_ids } = await bulkRetryTasks(taskIds);
      setSelected(new Set());
      setBulkNotice(`Retried ${new_task_ids.length} task${new_task_ids.length === 1 ? "" : "s"} as new runs.`);
      refresh();
    } catch (err) {
      setBulkError(err instanceof Error ? err.message : "Bulk retry failed.");
    } finally {
      setBulkRetrying(false);
    }
  }

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

      {status === "failed" && selected.size > 0 && (
        <div className="cm-bulk-bar">
          <span className="cm-bulk-bar-label">
            {selected.size} task{selected.size === 1 ? "" : "s"} selected
          </span>
          <button type="button" className="cm-btn cm-btn-solid" onClick={handleBulkRetry} disabled={bulkRetrying}>
            {bulkRetrying ? "Retrying…" : `Retry ${selected.size} selected`}
          </button>
        </div>
      )}
      {bulkNotice && (
        <p className="cm-notice" role="status">
          {bulkNotice}
        </p>
      )}
      {bulkError && (
        <p className="cm-error" role="alert">
          {bulkError}
        </p>
      )}

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
                  {status === "failed" && (
                    <th scope="col" className="cm-select-cell">
                      <span className="cm-visually-hidden">Select</span>
                    </th>
                  )}
                  <th scope="col">Task</th>
                  <th scope="col">Status</th>
                  <th scope="col">Started</th>
                </tr>
              </thead>
              <tbody>
                {tasks.map((t) => (
                  <tr key={t.task_id}>
                    {status === "failed" && (
                      <td className="cm-select-cell">
                        <input
                          type="checkbox"
                          checked={selected.has(t.task_id)}
                          onChange={() => toggle(t.task_id)}
                          aria-label={`Select ${t.task_name} (${t.task_id}) for bulk retry`}
                        />
                      </td>
                    )}
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
