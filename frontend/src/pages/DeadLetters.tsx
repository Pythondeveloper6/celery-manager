import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  listDeadLetters,
  rerunDeadLetters,
  type DeadLetter,
  type DeadLetterPage,
  type RerunResult,
} from "../api/deadletters";
import { FailureReview } from "../components/FailureReview";
import { TaskCallDialog } from "../components/TaskCallDialog";
import { IconCheck } from "../components/icons";
import { needsRole, useCan } from "../lib/role";
import "../styles/tasks.css";
import "../styles/deadletters.css";

const PAGE_SIZE = 50;
// Mirrors MAX_BULK_RERUN in control/deadletter.py.
const MAX_BULK_RERUN = 500;
// How many new-run links the notice spells out before saying "and N more".
const LINKS_SHOWN = 8;
// Summary groups shown as buttons; the filters reach the rest.
const GROUPS_SHOWN = 12;

const SINCE_OPTIONS = [
  { value: "", label: "Any time", hours: 0 },
  { value: "24h", label: "Last 24 hours", hours: 24 },
  { value: "7d", label: "Last 7 days", hours: 24 * 7 },
  { value: "30d", label: "Last 30 days", hours: 24 * 30 },
];

function formatTimestamp(value: string | null): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

export function DeadLetters() {
  const canOperate = useCan("operator");
  const [reviewing, setReviewing] = useState<string | null>(null);
  const [taskName, setTaskName] = useState("");
  const [exception, setException] = useState("");
  const [since, setSince] = useState("");
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<DeadLetterPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // Keyed by task id and holding the row, so a selection can span pages and
  // the edit dialog can be prefilled from any selected run.
  const [selected, setSelected] = useState<Map<string, DeadLetter>>(new Map());
  // The runs the edit dialog is open for.
  const [editing, setEditing] = useState<DeadLetter[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [result, setResult] = useState<RerunResult | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  // A "last 7 days" bound is an instant; fixing it per filter choice (and per
  // reload) keeps the request stable between renders.
  const sinceIso = useMemo(() => {
    const hours = SINCE_OPTIONS.find((o) => o.value === since)?.hours ?? 0;
    return hours && reloadKey >= 0 ? new Date(Date.now() - hours * 3_600_000).toISOString() : undefined;
  }, [since, reloadKey]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    listDeadLetters({
      task_name: taskName || undefined,
      exception: exception || undefined,
      since: sinceIso,
      limit: PAGE_SIZE,
      offset,
    })
      .then((data) => {
        if (cancelled) return;
        // A page past the end (its last rows were just run again) steps back.
        if (data.items.length === 0 && data.total > 0 && offset > 0) {
          setOffset(Math.floor((data.total - 1) / PAGE_SIZE) * PAGE_SIZE);
          return;
        }
        setPage(data);
        setError(null);
        setLoading(false);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : "Couldn't load the dead letters.");
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [taskName, exception, sinceIso, offset, reloadKey]);

  const reload = useCallback(() => setReloadKey((k) => k + 1), []);

  const items = useMemo(() => page?.items ?? [], [page]);
  const total = page?.total ?? 0;
  const groups = useMemo(() => page?.summary.groups ?? [], [page]);
  const taskNames = useMemo(() => [...new Set(groups.map((g) => g.task_name))].sort(), [groups]);
  const exceptionTypes = useMemo(() => [...new Set(groups.map((g) => g.exception_type))].sort(), [groups]);
  const filtersActive = Boolean(taskName || exception || since);

  const selectedRuns = [...selected.values()];
  const sameTask = new Set(selectedRuns.map((r) => r.task_name)).size === 1;
  const allOnPage = items.length > 0 && items.every((r) => selected.has(r.task_id));
  const tooMany = selected.size > MAX_BULK_RERUN;

  function changeFilter(apply: () => void) {
    apply();
    setOffset(0);
  }

  function clearFilters() {
    changeFilter(() => {
      setTaskName("");
      setException("");
      setSince("");
    });
  }

  function toggle(run: DeadLetter) {
    setSelected((prev) => {
      const next = new Map(prev);
      if (next.has(run.task_id)) next.delete(run.task_id);
      else next.set(run.task_id, run);
      return next;
    });
  }

  function togglePage() {
    setSelected((prev) => {
      const next = new Map(prev);
      for (const r of items) {
        if (allOnPage) next.delete(r.task_id);
        else next.set(r.task_id, r);
      }
      return next;
    });
  }

  function finish(outcome: RerunResult) {
    setResult(outcome);
    setActionError(null);
    // Whatever was run again is gone from the list; anything the broker
    // refused stays listed and stays selected so it can be tried again.
    const refused = new Set(outcome.errors.map((e) => e.task_id));
    setSelected((prev) => new Map([...prev].filter(([id]) => refused.has(id))));
    reload();
  }

  async function rerunSame(runs: DeadLetter[]) {
    setBusy(true);
    setActionError(null);
    setResult(null);
    try {
      finish(await rerunDeadLetters(runs.map((r) => r.task_id)));
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Couldn't run them again.");
    } finally {
      setBusy(false);
    }
  }

  const dispatchEdited = (runs: DeadLetter[]) => async (args: unknown[], kwargs: Record<string, unknown>) => {
    finish(await rerunDeadLetters(runs.map((r) => r.task_id), { args, kwargs }));
    // The dialog hands this to onDone, which has nothing to open: the new
    // runs are linked in the notice instead.
    return "";
  };

  const first = editing?.[0];

  return (
    <div className="cm-page">
      <header className="cm-page-header">
        <div className="cm-page-header-titles">
          <h1 className="cm-page-title">Dead letters</h1>
          <p className="cm-page-lede">
            Runs that failed after their last retry. Fix the arguments and run them again, one at a time or many at once.
          </p>
        </div>
      </header>

      <FailureReview taskId={reviewing} onChanged={reload} />
      {result && (
        <div className="cm-notice cm-dl-result" role="status">
          <div>
            {result.reruns.length > 0 && (
              <p>
                Started {result.reruns.length.toLocaleString()} new run{result.reruns.length === 1 ? "" : "s"}:{" "}
                {result.reruns.slice(0, LINKS_SHOWN).map((r, i) => (
                  <span key={r.new_task_id}>
                    {i > 0 && ", "}
                    <Link to={`/tasks/${r.new_task_id}`} className="cm-link">
                      {r.new_task_id.slice(0, 8)}
                    </Link>
                  </span>
                ))}
                {result.reruns.length > LINKS_SHOWN && ` and ${result.reruns.length - LINKS_SHOWN} more`}.
              </p>
            )}
            {result.errors.length > 0 && (
              <p>
                {result.errors.length.toLocaleString()} could not be sent and are still listed:{" "}
                {result.errors
                  .slice(0, 3)
                  .map((e) => `${e.task_id.slice(0, 8)} (${e.error})`)
                  .join("; ")}
                .
              </p>
            )}
          </div>
          <button type="button" className="cm-btn cm-btn-ghost cm-btn-sm" onClick={() => setResult(null)}>
            Dismiss
          </button>
        </div>
      )}
      {actionError && (
        <p className="cm-error" role="alert">
          {actionError}
        </p>
      )}

      {groups.length > 0 && (
        <section className="cm-card cm-card-pad cm-dl-summary" aria-label="Dead letters by task and exception">
          <h2 className="cm-dl-summary-title">
            {(page?.summary.total ?? 0).toLocaleString()} dead letter{page?.summary.total === 1 ? "" : "s"}
            {page?.summary.truncated ? " (the summary covers the newest only)" : ""}
          </h2>
          <ul className="cm-dl-groups">
            {groups.slice(0, GROUPS_SHOWN).map((g) => {
              const active = taskName === g.task_name && exception === g.exception_type;
              return (
                <li key={`${g.task_name}|${g.exception_type}`}>
                  <button
                    type="button"
                    className="cm-dl-group"
                    aria-pressed={active}
                    onClick={() =>
                      changeFilter(() => {
                        setTaskName(active ? "" : g.task_name);
                        setException(active ? "" : g.exception_type);
                      })
                    }
                  >
                    <span className="cm-dl-group-count">{g.count.toLocaleString()}</span>
                    <span className="cm-dl-group-text">
                      <span className="cm-dl-group-task">{g.task_name}</span>
                      <span className="cm-dl-group-exc">{g.exception_type}</span>
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
          {groups.length > GROUPS_SHOWN && (
            <p className="cm-field-hint">
              The {GROUPS_SHOWN} largest of {groups.length.toLocaleString()} groups. The filters below reach the rest.
            </p>
          )}
        </section>
      )}

      <div className="cm-tasks-toolbar" role="group" aria-label="Filters">
        <select
          className="cm-select cm-dl-filter"
          aria-label="Task"
          value={taskName}
          onChange={(e) => changeFilter(() => setTaskName(e.target.value))}
        >
          <option value="">All tasks</option>
          {taskNames.map((n) => (
            <option key={n} value={n}>
              {n}
            </option>
          ))}
        </select>
        <select
          className="cm-select cm-dl-filter"
          aria-label="Exception"
          value={exception}
          onChange={(e) => changeFilter(() => setException(e.target.value))}
        >
          <option value="">All exceptions</option>
          {exceptionTypes.map((n) => (
            <option key={n} value={n}>
              {n}
            </option>
          ))}
        </select>
        <select
          className="cm-select cm-dl-filter"
          aria-label="Failed within"
          value={since}
          onChange={(e) => changeFilter(() => setSince(e.target.value))}
        >
          {SINCE_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        {filtersActive && (
          <button type="button" className="cm-btn cm-btn-ghost cm-btn-sm" onClick={clearFilters}>
            Clear filters
          </button>
        )}
      </div>

      {canOperate && selected.size > 0 && (
        <div className="cm-bulk-bar" role="group" aria-label="Actions for the selected runs">
          <span className="cm-bulk-bar-label">
            {selected.size.toLocaleString()} selected
            {tooMany && ` (the limit is ${MAX_BULK_RERUN} at a time)`}
          </span>
          <div className="cm-bulk-actions">
            <button type="button" className="cm-btn cm-btn-ghost cm-btn-sm" onClick={() => setSelected(new Map())}>
              Clear selection
            </button>
            <button
              type="button"
              className="cm-btn cm-btn-outline cm-btn-sm"
              disabled={!sameTask || tooMany || busy}
              title={sameTask ? undefined : "Editing arguments needs runs of a single task. Select one task's runs."}
              onClick={() => setEditing(selectedRuns)}
            >
              Run again with new arguments…
            </button>
            <button
              type="button"
              className="cm-btn cm-btn-primary cm-btn-sm"
              disabled={tooMany || busy}
              aria-busy={busy}
              onClick={() => rerunSame(selectedRuns)}
            >
              {busy ? "Starting…" : `Run ${selected.size.toLocaleString()} again, same arguments`}
            </button>
          </div>
        </div>
      )}

      <div className="cm-card cm-card-flush">
        {loading && !page ? (
          <p className="cm-empty-state" role="status">
            Loading dead letters&hellip;
          </p>
        ) : error ? (
          <div className="cm-card-pad cm-stack">
            <p className="cm-error" role="alert">
              {error}
            </p>
            <button type="button" className="cm-btn cm-btn-outline" onClick={reload}>
              Try again
            </button>
          </div>
        ) : items.length === 0 ? (
          filtersActive ? (
            <div className="cm-tasks-empty">
              <p className="cm-tasks-empty-title">No dead letters match these filters</p>
              <p className="cm-tasks-empty-body">Drop a filter to see more.</p>
              <button type="button" className="cm-btn cm-btn-outline" onClick={clearFilters}>
                Clear filters
              </button>
            </div>
          ) : (
            <div className="cm-tasks-empty">
              <p className="cm-tasks-empty-title">No dead letters</p>
              <p className="cm-tasks-empty-body">
                Nothing has failed for good, or every failed run has been run again. A run appears here once it has
                failed and used up its retries.
              </p>
            </div>
          )
        ) : (
          <>
            <div className="cm-table-scroll" aria-busy={loading}>
              <table className="cm-table cm-table-stack cm-dl-table">
                <thead>
                  <tr>
                    {canOperate && (
                      <th scope="col" className="cm-select-cell">
                        <span className="cm-checkbox">
                          <input
                            type="checkbox"
                            checked={allOnPage}
                            onChange={togglePage}
                            aria-label="Select every run on this page"
                          />
                          <IconCheck />
                        </span>
                      </th>
                    )}
                    <th scope="col">Task</th>
                    <th scope="col">Error</th>
                    <th scope="col" className="cm-col-fit" title="Runs that came before this one in its retry chain">
                      Earlier runs
                    </th>
                    <th scope="col" className="cm-col-fit">
                      Failed
                    </th>
                    {canOperate && (
                      <th scope="col" className="cm-col-fit">
                        <span className="cm-visually-hidden">Actions</span>
                      </th>
                    )}
                  </tr>
                </thead>
                <tbody>
                  {items.map((r) => (
                    <tr key={r.task_id} data-status="failed">
                      {canOperate && (
                        <td className="cm-select-cell">
                          <span className="cm-checkbox">
                            <input
                              type="checkbox"
                              checked={selected.has(r.task_id)}
                              onChange={() => toggle(r)}
                              aria-label={`Select ${r.task_name} (${r.task_id})`}
                            />
                            <IconCheck />
                          </span>
                        </td>
                      )}
                      <td className="cm-cell-primary">
                        <button type="button" className="cm-btn cm-btn-ghost cm-btn-sm" onClick={() => setReviewing(r.task_id)}>Review failure</button>
                        <Link to={`/tasks/${r.task_id}`} className="cm-task-link" title={r.task_id}>
                          <span className="cm-task-name">{r.task_name}</span>
                          <span className="cm-task-id">{r.task_id.slice(0, 8)}</span>
                        </Link>
                      </td>
                      <td>
                        <span className="cm-dl-exc-type">{r.exception_type}</span>
                        {r.last_line && <span className="cm-dl-exc-line">{r.last_line}</span>}
                      </td>
                      <td className="cm-col-fit cm-dl-num">{r.earlier_attempts}</td>
                      <td className="cm-col-fit cm-timestamp">{formatTimestamp(r.finished_at ?? r.started_at)}</td>
                      {canOperate && (
                        <td className="cm-col-fit">
                          <button
                            type="button"
                            className="cm-btn cm-btn-outline cm-btn-sm"
                            onClick={() => setEditing([r])}
                          >
                            Edit arguments and run again
                          </button>
                        </td>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div className="cm-tasks-foot">
              <span className="cm-tasks-count">
                {`${(offset + 1).toLocaleString()}–${(offset + items.length).toLocaleString()} of ${total.toLocaleString()}`}
                {!canOperate && <span title={needsRole("operator")}> &middot; running them again needs the operator role</span>}
              </span>
              {total > PAGE_SIZE && (
                <div className="cm-tasks-pager">
                  <button
                    type="button"
                    className="cm-btn cm-btn-outline cm-btn-sm"
                    onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                    disabled={offset === 0 || loading}
                  >
                    Newer
                  </button>
                  <span className="cm-tasks-pager-position">
                    Page {(Math.floor(offset / PAGE_SIZE) + 1).toLocaleString()} of{" "}
                    {Math.ceil(total / PAGE_SIZE).toLocaleString()}
                  </span>
                  <button
                    type="button"
                    className="cm-btn cm-btn-outline cm-btn-sm"
                    onClick={() => setOffset(offset + PAGE_SIZE)}
                    disabled={offset + PAGE_SIZE >= total || loading}
                  >
                    Older
                  </button>
                </div>
              )}
            </div>
          </>
        )}
      </div>

      {editing && first && (
        <TaskCallDialog
          mode="retry"
          retryOf={{ taskId: first.task_id, taskName: first.task_name, args: first.args, kwargs: first.kwargs }}
          title={editing.length === 1 ? "Edit arguments and run again" : `Run ${editing.length} again with new arguments`}
          description={
            editing.length === 1
              ? "Edit the arguments, then run this again. The failed run is kept, linked to the new one, and leaves this list."
              : `These arguments are used for all ${editing.length} runs, prefilled from the first. The failed runs are kept, linked to the new ones, and leave this list.`
          }
          submitLabel={editing.length === 1 ? "Run again with these arguments" : `Run ${editing.length} again`}
          dispatch={dispatchEdited(editing)}
          onClose={() => setEditing(null)}
          onDone={() => setEditing(null)}
        />
      )}
    </div>
  );
}
