import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { Link } from "react-router-dom";
import {
  addConsumer,
  cancelConsumer,
  growPool,
  listRateLimits,
  listWorkers,
  RATE_LIMIT_PATTERN,
  setRateLimit,
  shrinkPool,
  shutdownWorker,
  type ControlResult,
  type TaskRateLimit,
  type Worker,
  type WorkerSnapshot,
} from "../api/workers";
import { WorkerHeartbeatHistory } from "../components/WorkerHeartbeatHistory";
import { IconX } from "../components/icons";
import "../styles/workers.css";
import { useCan } from "../lib/role";

// Each refresh is a broadcast that can take up to the server's inspect
// timeout (1 s by default) per round, so polling faster than this mostly
// queues requests behind each other.
const REFRESH_MS = 10_000;
const CONFIRM_MS = 5000;

type Note = { kind: "ok" | "error"; text: string };

function formatUptime(seconds: number | null): string {
  if (seconds == null) return "—";
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m`;
  return `${Math.floor(seconds)}s`;
}

function formatAgo(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 5) return "just now";
  if (s < 60) return `${s}s ago`;
  return `${Math.floor(s / 60)}m ago`;
}

/** Turn a control reply into one sentence an operator can act on. */
function describeResult(result: ControlResult, success: string, timeout: number | undefined): Note {
  const refused = Object.entries(result.errors);
  if (refused.length) {
    return { kind: "error", text: refused.map(([host, msg]) => `${host} refused: ${msg}`).join(" ") };
  }
  if (!result.acknowledged.length) {
    return {
      kind: "error",
      text: `No worker confirmed within ${timeout ?? 1} s. It may still apply; check again after the next refresh.`,
    };
  }
  return { kind: "ok", text: success };
}

// --------------------------------------------------------------------------
// One worker card
// --------------------------------------------------------------------------

function WorkerCard({
  worker,
  timeout,
  onChanged,
}: {
  worker: Worker;
  timeout: number | undefined;
  onChanged: () => void;
}) {
  // Pool, queue and rate-limit controls are operator; shutdown is admin
  // (it can't be undone from here). Cosmetic: the server enforces both.
  const canOperate = useCan("operator");
  const canShutdown = useCan("admin");
  const [note, setNote] = useState<Note | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [armed, setArmed] = useState(false);
  const [queue, setQueue] = useState("");
  const armTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(
    () => () => {
      if (armTimer.current) clearTimeout(armTimer.current);
    },
    [],
  );

  async function run(key: string, action: () => Promise<Note>) {
    setBusy(key);
    setNote(null);
    try {
      setNote(await action());
      onChanged();
    } catch (err) {
      setNote({ kind: "error", text: err instanceof Error ? err.message : "That didn't work. Try again." });
    } finally {
      setBusy(null);
    }
  }

  function arm() {
    setArmed(true);
    armTimer.current = setTimeout(() => setArmed(false), CONFIRM_MS);
  }

  function disarm() {
    if (armTimer.current) clearTimeout(armTimer.current);
    setArmed(false);
  }

  function shutdown() {
    disarm();
    run("shutdown", async () => {
      await shutdownWorker(worker.name);
      return {
        kind: "ok",
        text: "Shutdown sent. The worker finishes its running tasks, then exits and drops off this list.",
      };
    });
  }

  function submitQueue(event: FormEvent) {
    event.preventDefault();
    const name = queue.trim();
    if (!name) return;
    run("queue-add", async () => {
      const note = describeResult(await addConsumer(worker.name, name), `Now consuming from ${name}.`, timeout);
      if (note.kind === "ok") setQueue("");
      return note;
    });
  }

  // Live child processes when the pool reports them (prefork), else the
  // configured concurrency (threads, gevent...).
  const size = worker.pool_processes ?? worker.concurrency;
  const poolLabel = worker.pool ? `${worker.pool} pool${size != null ? ` × ${size}` : ""}` : null;

  return (
    <article className="cm-card cm-worker" aria-labelledby={`worker-${worker.name}`}>
      <header className="cm-worker-head">
        <div className="cm-worker-titles">
          <h2 className="cm-worker-name" id={`worker-${worker.name}`}>
            {worker.name}
          </h2>
          <p className="cm-worker-meta">
            {worker.pid != null && <span>PID {worker.pid}</span>}
            <span>Up {formatUptime(worker.uptime_seconds)}</span>
            {poolLabel && <span>{poolLabel}</span>}
          </p>
        </div>
        <span className="cm-status" data-status={worker.online ? "succeeded" : "queued"}>
          {worker.online ? "Online" : "Not responding"}
        </span>
      </header>

      <dl className="cm-worker-stats">
        <div>
          <dt>Processed</dt>
          <dd>{worker.processed_total.toLocaleString()}</dd>
        </div>
        <div>
          <dt>Active</dt>
          <dd>{worker.active_count}</dd>
        </div>
        <div>
          <dt>Reserved</dt>
          <dd>{worker.reserved_count}</dd>
        </div>
        <div>
          <dt>Scheduled</dt>
          <dd>{worker.scheduled_count}</dd>
        </div>
      </dl>

      <section className="cm-worker-section" aria-label="Running tasks">
        <h3 className="cm-worker-label">Running now</h3>
        {worker.active.length === 0 ? (
          <p className="cm-worker-muted">Idle.</p>
        ) : (
          <ul className="cm-worker-tasks">
            {worker.active.map((t, i) => (
              <li key={t.id ?? i}>
                {t.id ? (
                  <Link to={`/tasks/${encodeURIComponent(t.id)}`} className="cm-link">
                    {t.name ?? t.id}
                  </Link>
                ) : (
                  t.name
                )}
                {t.id && <span className="cm-worker-taskid">{t.id.slice(0, 8)}</span>}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="cm-worker-section" aria-label="Queues">
        <h3 className="cm-worker-label">Queues</h3>
        {worker.queues.length === 0 ? (
          <p className="cm-worker-muted">Not consuming from any queue.</p>
        ) : (
          <ul className="cm-worker-queues">
            {worker.queues.map((q) => (
              <li key={q} className="cm-worker-queue">
                <span>{q}</span>
                {canOperate && (
                  <button
                    type="button"
                    className="cm-worker-queue-remove"
                    aria-label={`Stop consuming from ${q}`}
                    title={`Stop consuming from ${q}`}
                    disabled={busy !== null}
                    onClick={() =>
                      run(`queue-cancel-${q}`, async () =>
                        describeResult(
                          await cancelConsumer(worker.name, q),
                          `Stopped consuming from ${q}. Restarting the worker restores its -Q queues.`,
                          timeout,
                        ),
                      )
                    }
                  >
                    <IconX className="cm-worker-queue-icon" />
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
        {canOperate && (
          <form className="cm-worker-queue-form" onSubmit={submitQueue}>
            <label className="cm-visually-hidden" htmlFor={`queue-${worker.name}`}>
              Queue to consume from
            </label>
            <input
              id={`queue-${worker.name}`}
              className="cm-input cm-worker-queue-input"
              placeholder="Queue name"
              value={queue}
              maxLength={255}
              onChange={(e) => setQueue(e.target.value)}
            />
            <button type="submit" className="cm-btn cm-btn-outline cm-btn-sm" disabled={!queue.trim() || busy !== null}>
              {busy === "queue-add" ? "Adding…" : "Consume queue"}
            </button>
          </form>
        )}
      </section>

      {note && (
        <p className={note.kind === "ok" ? "cm-notice" : "cm-error"} role={note.kind === "ok" ? "status" : "alert"}>
          {note.text}
        </p>
      )}

      {canOperate && (
        <footer className="cm-worker-actions">
          <button
            type="button"
            className="cm-btn cm-btn-outline cm-btn-sm"
            disabled={busy !== null}
            onClick={() =>
              run("grow", async () => describeResult(await growPool(worker.name), "Pool will grow by one process.", timeout))
            }
          >
            {busy === "grow" ? "Growing…" : "Add process"}
          </button>
          <button
            type="button"
            className="cm-btn cm-btn-outline cm-btn-sm"
            disabled={busy !== null}
            onClick={() =>
              run("shrink", async () =>
                describeResult(await shrinkPool(worker.name), "Pool will shrink by one process.", timeout),
              )
            }
          >
            {busy === "shrink" ? "Shrinking…" : "Remove process"}
          </button>
          <span className="cm-worker-actions-spacer" />
          {!canShutdown ? null : busy === "shutdown" ? (
            <button type="button" className="cm-btn cm-btn-outline cm-btn-sm" disabled>
              Shutting down…
            </button>
          ) : armed ? (
            <span className="cm-worker-confirm" role="group" aria-label={`Confirm shutting down ${worker.name}`}>
              <button type="button" className="cm-btn cm-btn-destructive cm-btn-sm" onClick={shutdown}>
                Confirm shutdown
              </button>
              <button type="button" className="cm-btn cm-btn-ghost cm-btn-sm" onClick={disarm}>
                Keep running
              </button>
            </span>
          ) : (
            <button type="button" className="cm-btn cm-btn-destructive-outline cm-btn-sm" disabled={busy !== null} onClick={arm}>
              Shut down
            </button>
          )}
        </footer>
      )}
      <WorkerHeartbeatHistory hostname={worker.name} />
    </article>
  );
}

// --------------------------------------------------------------------------
// Rate limits
// --------------------------------------------------------------------------

function RateLimitRow({
  row,
  timeout,
  workerCount,
}: {
  row: TaskRateLimit;
  timeout: number | undefined;
  workerCount: number | null;
}) {
  const canOperate = useCan("operator");
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(row.rate_limit ?? "");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<Note | null>(null);
  // What this session last applied, so the row can say so. Workers don't
  // report their current limit back, and it is lost when they restart.
  const [applied, setApplied] = useState<string | null>(null);

  const trimmed = value.trim();
  const invalid = trimmed !== "" && !RATE_LIMIT_PATTERN.test(trimmed);

  async function apply(next: string) {
    setBusy(true);
    setNote(null);
    try {
      const result = await setRateLimit(row.task_name, next);
      const n = result.acknowledged.length;
      const done = next ? `Set to ${next}` : "Limit removed";
      const note = describeResult(
        result,
        `${done} on ${n} worker${n === 1 ? "" : "s"}: ${result.acknowledged.join(", ")}.`,
        timeout,
      );
      setNote(note);
      if (n) setApplied(next || "none");
      if (note.kind === "ok") setEditing(false);
    } catch (err) {
      setNote({ kind: "error", text: err instanceof Error ? err.message : "Couldn't change the rate limit." });
    } finally {
      setBusy(false);
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (invalid) return;
    apply(trimmed);
  }

  const inputId = `rate-${row.task_name}`;

  return (
    <>
      <tr className={editing || note ? "cm-rate-row-open" : undefined}>
        <td className="cm-cell-primary cm-rate-task">{row.task_name}</td>
        <td className="cm-rate-default">
          <span className="cm-rate-tag">{row.rate_limit ?? "Unlimited"}</span>
          {applied && <span className="cm-rate-applied">Runtime: {applied === "none" ? "unlimited" : applied}</span>}
        </td>
        <td className="cm-rate-edit">
          {canOperate && !editing && (
            <button
              type="button"
              className="cm-btn cm-btn-outline cm-btn-sm"
              disabled={workerCount === 0}
              title={workerCount === 0 ? "No workers are running to apply it to" : undefined}
              onClick={() => {
                setEditing(true);
                setNote(null);
              }}
            >
              Change limit
            </button>
          )}
        </td>
      </tr>
      {(editing || note) && (
        <tr className="cm-rate-detail">
          <td colSpan={3}>
            {editing && (
              <form className="cm-rate-form" onSubmit={submit}>
                <label className="cm-rate-label" htmlFor={inputId}>
                  New limit for {row.task_name}
                </label>
                <input
                  id={inputId}
                  className="cm-input cm-rate-input"
                  placeholder="e.g. 10/m"
                  value={value}
                  maxLength={32}
                  autoFocus
                  aria-invalid={invalid}
                  aria-describedby={`${inputId}-hint`}
                  onChange={(e) => setValue(e.target.value)}
                />
                <button type="submit" className="cm-btn cm-btn-primary cm-btn-sm" disabled={busy || invalid}>
                  {busy ? "Applying…" : trimmed ? "Apply limit" : "Remove limit"}
                </button>
                <button type="button" className="cm-btn cm-btn-ghost cm-btn-sm" disabled={busy} onClick={() => setEditing(false)}>
                  Cancel
                </button>
                <p className={invalid ? "cm-field-error cm-rate-hint" : "cm-field-hint cm-rate-hint"} id={`${inputId}-hint`}>
                  Tasks per second, minute or hour: 10/s, 10/m, 100/h. Leave empty to remove the limit. Applies to
                  every running worker until it restarts.
                </p>
              </form>
            )}
            {note && (
              <p className={note.kind === "ok" ? "cm-notice cm-rate-note" : "cm-error cm-rate-note"} role={note.kind === "ok" ? "status" : "alert"}>
                {note.text}
              </p>
            )}
          </td>
        </tr>
      )}
    </>
  );
}

function RateLimits({ timeout, workerCount }: { timeout: number | undefined; workerCount: number | null }) {
  const [rows, setRows] = useState<TaskRateLimit[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setRows(await listRateLimits());
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't load registered tasks.");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <section className="cm-card cm-card-flush" aria-labelledby="rate-limits-title">
      <div className="cm-card-pad cm-card-header">
        <h2 className="cm-card-title" id="rate-limits-title">
          Rate limits
        </h2>
        <p className="cm-card-description">
          Change how often a task may start on each running worker. Changes are not saved: a worker that restarts,
          and any worker started later, goes back to the limit declared in code.
        </p>
      </div>
      {error ? (
        <div className="cm-card-pad cm-stack">
          <p className="cm-error" role="alert">
            {error}
          </p>
          <button type="button" className="cm-btn cm-btn-outline" onClick={load}>
            Try again
          </button>
        </div>
      ) : rows === null ? (
        <div className="cm-card-pad">
          <p className="cm-loading">
            <span className="cm-live-dot" aria-hidden="true" />
            Loading tasks&hellip;
          </p>
        </div>
      ) : rows.length === 0 ? (
        <p className="cm-empty-state">
          No tasks are registered with this dashboard&rsquo;s Celery app. Import your task modules (or call
          autodiscover) in the process that serves it.
        </p>
      ) : (
        <div className="cm-table-scroll">
          <table className="cm-table cm-table-stack cm-rate-table">
            <thead>
              <tr>
                <th scope="col">Task</th>
                <th scope="col">Declared limit</th>
                <th scope="col">
                  <span className="cm-visually-hidden">Change</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <RateLimitRow key={r.task_name} row={r} timeout={timeout} workerCount={workerCount} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

// --------------------------------------------------------------------------
// Page
// --------------------------------------------------------------------------

function useDocumentVisible(): boolean {
  const [visible, setVisible] = useState(() => !document.hidden);
  useEffect(() => {
    const onChange = () => setVisible(!document.hidden);
    document.addEventListener("visibilitychange", onChange);
    return () => document.removeEventListener("visibilitychange", onChange);
  }, []);
  return visible;
}

export function Workers() {
  const [snapshot, setSnapshot] = useState<WorkerSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const inflight = useRef(false);
  const visible = useDocumentVisible();

  const refresh = useCallback(async () => {
    // A slow broker can make one round outlast the interval; don't stack them.
    if (inflight.current) return;
    inflight.current = true;
    setLoading(true);
    try {
      setSnapshot(await listWorkers());
      setError(null);
      setUpdatedAt(Date.now());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't load workers.");
    } finally {
      inflight.current = false;
      setLoading(false);
    }
  }, []);

  // Poll only while the tab is visible; catch up at once when it returns.
  useEffect(() => {
    if (!visible) return;
    refresh();
    const id = setInterval(refresh, REFRESH_MS);
    return () => clearInterval(id);
  }, [visible, refresh]);

  // Ticks the "updated Xs ago" label.
  useEffect(() => {
    if (!visible) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [visible]);

  const workers = snapshot?.workers ?? null;
  const timeout = snapshot?.timeout_seconds;

  return (
    <div className="cm-page">
      <header className="cm-page-header">
        <div className="cm-page-header-titles">
          <h1 className="cm-page-title">Workers</h1>
          <p className="cm-page-lede">Live state of every worker that answers the broker. Refreshes every 10 seconds.</p>
        </div>
        <div className="cm-workers-toolbar">
          {!visible ? (
            <span className="cm-live-indicator" title="Refreshing resumes when you come back to this tab">
              <span className="cm-paused-dot" aria-hidden="true" />
              Paused
            </span>
          ) : error ? (
            <span className="cm-live-indicator" title={error}>
              <span className="cm-paused-dot" aria-hidden="true" />
              {updatedAt ? `Last answer ${formatAgo(now - updatedAt)}` : "Not connected"}
            </span>
          ) : (
            <span className="cm-live-indicator" title="Refreshes every 10 seconds while this tab is open">
              <span className="cm-live-dot" aria-hidden="true" />
              {updatedAt ? `Updated ${formatAgo(now - updatedAt)}` : "Connecting…"}
            </span>
          )}
          <button type="button" className="cm-btn cm-btn-outline cm-btn-sm" onClick={refresh} disabled={loading}>
            {loading ? "Refreshing…" : "Refresh now"}
          </button>
        </div>
      </header>

      {error && workers && (
        <p className="cm-error" role="alert">
          {error} Showing the last answer, from {updatedAt ? formatAgo(now - updatedAt) : "earlier"}.
        </p>
      )}

      {workers === null ? (
        <div className="cm-card">
          {error ? (
            <div className="cm-stack">
              <p className="cm-error" role="alert">
                {error}
              </p>
              <button type="button" className="cm-btn cm-btn-outline" onClick={refresh}>
                Try again
              </button>
            </div>
          ) : (
            <p className="cm-loading">
              <span className="cm-live-dot" aria-hidden="true" />
              Asking workers&hellip;
            </p>
          )}
        </div>
      ) : workers.length === 0 ? (
        <div className="cm-card cm-workers-empty">
          <h2 className="cm-card-title">No workers answered</h2>
          <p className="cm-card-description">
            Nothing replied within {timeout ?? 1} s. Start a worker against the same broker as this dashboard, for
            example <code>celery -A proj worker</code>, and it appears here on the next refresh. Workers must have
            remote control enabled (the default) to be listed.
          </p>
        </div>
      ) : (
        <div className="cm-workers-grid">
          {workers.map((w) => (
            <WorkerCard key={w.name} worker={w} timeout={timeout} onChanged={refresh} />
          ))}
        </div>
      )}

      <RateLimits timeout={timeout} workerCount={workers ? workers.length : null} />
    </div>
  );
}
