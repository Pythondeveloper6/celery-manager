import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  cancelDelayed,
  listDelayed,
  listUnacked,
  MAX_DELAYED_CANCEL,
  MAX_RESTORE,
  restoreUnacked,
  type DelayedListing,
  type DelayedTask,
  type MaskedCall,
  type UnackedEntry,
  type UnackedListing,
  type UnackedState,
} from "../api/held";
import { IconCheck } from "../components/icons";
import { StatusBadge } from "../components/Status";
import { needsRole, useCan } from "../lib/role";
import "../styles/tasks.css";
import "../styles/held-tasks.css";

/* ==========================================================================
   Held tasks: tasks that have left their queue but haven't run.

   One page with two tabs rather than a tab on Workers and another on Queues,
   because both answer the same question — "my task isn't on the queue and
   isn't running; where is it?" — and an operator chasing a missing task
   shouldn't have to know which mechanism holds it to find it:

   - Delayed: ETA/countdown tasks a worker took off the queue and keeps in
     memory until they're due (any broker).
   - Stuck: messages a worker took but never acknowledged, which on Redis can
     sit unseen for the visibility timeout after their worker dies.
   ========================================================================== */

type Tab = "delayed" | "stuck";

const DELAYED_REFRESH_MS = 5000;
// Each stuck-tasks refresh pings every worker (about a second), so less often.
const STUCK_REFRESH_MS = 10000;

/** "2d 4h", "3h 12m", "4m 05s", "12s". */
function formatDuration(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  const days = Math.floor(s / 86400);
  const hours = Math.floor((s % 86400) / 3600);
  const minutes = Math.floor((s % 3600) / 60);
  const seconds = s % 60;
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
  return `${seconds}s`;
}

function formatTime(value: string | null): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit", second: "2-digit" });
}

function plural(n: number, word: string): string {
  return `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;
}

/** Ticks once a second, for countdowns and ages between refreshes. */
function useNow(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const interval = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(interval);
  }, []);
  return now;
}

function usePageVisible(): boolean {
  const [visible, setVisible] = useState(() => document.visibilityState === "visible");
  useEffect(() => {
    const onVisibility = () => setVisible(document.visibilityState === "visible");
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, []);
  return visible;
}

/** Loads `fetcher` now and every `ms` while the tab is visible. A late
 *  answer to an older request never overwrites a newer one. */
function usePolled<T>(fetcher: () => Promise<T>, ms: number) {
  const visible = usePageVisible();
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const requestIdRef = useRef(0);

  const refresh = useCallback(async () => {
    const requestId = ++requestIdRef.current;
    try {
      const next = await fetcher();
      if (requestId !== requestIdRef.current) return;
      setData(next);
      setError(null);
    } catch (err) {
      if (requestId !== requestIdRef.current) return;
      setError(err instanceof Error ? err.message : "Couldn't load.");
    }
  }, [fetcher]);

  useEffect(() => {
    if (!visible) return;
    refresh();
    const interval = setInterval(refresh, ms);
    return () => clearInterval(interval);
  }, [visible, refresh, ms]);

  return { data, error, refresh, visible };
}

function LiveIndicator({ visible, ms }: { visible: boolean; ms: number }) {
  return visible ? (
    <div className="cm-live-indicator" title={`Refreshes every ${ms / 1000} seconds`}>
      <span className="cm-live-dot" aria-hidden="true" />
      Live
    </div>
  ) : (
    <div className="cm-live-indicator" title="Paused while this tab is hidden">
      <span className="cm-paused-dot" aria-hidden="true" />
      Paused
    </div>
  );
}

function TaskCell({ taskId, taskName }: { taskId: string | null; taskName: string | null }) {
  const name = <span className="cm-task-name">{taskName ?? "Unknown task"}</span>;
  if (!taskId) return name;
  return (
    <Link to={`/tasks/${encodeURIComponent(taskId)}`} className="cm-task-link" title={taskId}>
      {name}
      <span className="cm-task-id">{taskId.slice(0, 8)}</span>
    </Link>
  );
}

function maskedText(masked: MaskedCall): string {
  const args = masked.args.join(", ");
  const kwargs = Object.entries(masked.kwargs)
    .map(([k, v]) => `${k}: ${v}`)
    .join(", ");
  return [`(${args})`, kwargs ? `{${kwargs}}` : ""].filter(Boolean).join(" ");
}

function revealedText(task: DelayedTask): string {
  try {
    return `${JSON.stringify(task.args ?? [])} ${JSON.stringify(task.kwargs ?? {})}`;
  } catch {
    return "(not displayable)";
  }
}

/* ---- Delayed --------------------------------------------------------------- */

function CancelConfirm({
  ids,
  onKeep,
  onCancelled,
}: {
  ids: string[];
  onKeep: () => void;
  onCancelled: (message: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const many = ids.length > 1;

  async function run() {
    setBusy(true);
    setError(null);
    try {
      const result = await cancelDelayed(ids);
      const marked =
        result.runs_marked_revoked > 0 ? ` ${plural(result.runs_marked_revoked, "recorded run")} marked revoked.` : "";
      onCancelled(`Cancelled ${plural(result.revoked, "delayed task")}.${marked}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't cancel.");
      setBusy(false);
    }
  }

  return (
    <div className="cm-held-confirm" role="group" aria-label="Confirm cancelling" onKeyDown={(e) => e.key === "Escape" && onKeep()}>
      <p className="cm-held-confirm-text">
        Cancel {many ? plural(ids.length, "delayed task") : "this delayed task"}? The worker holding{" "}
        {many ? "them" : "it"} drops {many ? "them" : "it"} at the ETA instead of running {many ? "them" : "it"}, and
        recorded runs are marked revoked now. A cancel lives in each worker&rsquo;s memory: a worker restarted before
        the ETA forgets it and runs the task anyway, unless workers run with <code>--statedb</code>.
      </p>
      <div className="cm-held-confirm-row">
        <button type="button" className="cm-btn cm-btn-outline cm-btn-sm" onClick={onKeep} disabled={busy}>
          Keep {many ? "them" : "it"}
        </button>
        <button type="button" className="cm-btn cm-btn-destructive cm-btn-sm" onClick={run} disabled={busy} aria-busy={busy}>
          {busy ? "Cancelling…" : many ? `Cancel ${ids.length.toLocaleString()} tasks` : "Cancel task"}
        </button>
      </div>
      {error && (
        <p className="cm-error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

function DelayedPanel() {
  const canOperate = useCan("operator");
  const [reveal, setReveal] = useState(false);
  const fetcher = useCallback(() => listDelayed(reveal), [reveal]);
  const { data, error, refresh, visible } = usePolled<DelayedListing>(fetcher, DELAYED_REFRESH_MS);
  const now = useNow();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirming, setConfirming] = useState<string[] | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // A cancelled task stays in the worker's schedule until its ETA; its run
  // reading `revoked` is how the page knows not to offer cancel again.
  const cancellable = (t: DelayedTask) => t.run?.status !== "revoked";
  const tasks = data?.tasks ?? [];
  const selectable = tasks.filter(cancellable);
  // Drop ids from the selection once the worker no longer holds them.
  const liveSelected = [...selected].filter((id) => selectable.some((t) => t.task_id === id));
  const allSelected = selectable.length > 0 && liveSelected.length === selectable.length;
  const tooMany = liveSelected.length > MAX_DELAYED_CANCEL;

  function toggle(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <section className="cm-stack" aria-labelledby="cm-held-delayed-heading">
      <div className="cm-held-toolbar">
        <h2 id="cm-held-delayed-heading" className="cm-visually-hidden">
          Delayed tasks
        </h2>
        <p className="cm-held-lede">Countdown and ETA tasks a worker has taken off the queue and is holding until they&rsquo;re due.</p>
        <div className="cm-held-toolbar-end">
          {canOperate && (
            <label className="cm-held-reveal">
              <span className="cm-checkbox">
                <input type="checkbox" checked={reveal} onChange={(e) => setReveal(e.target.checked)} />
                <IconCheck />
              </span>
              Show argument values
            </label>
          )}
          <LiveIndicator visible={visible} ms={DELAYED_REFRESH_MS} />
        </div>
      </div>

      {notice && (
        <p className="cm-notice" role="status">
          {notice}
        </p>
      )}

      {canOperate && liveSelected.length > 0 && !confirming && (
        <div className="cm-bulk-bar" role="group" aria-label="Actions for the selected tasks">
          <span className="cm-bulk-bar-label">
            {liveSelected.length.toLocaleString()} selected
            {tooMany && ` (the limit is ${MAX_DELAYED_CANCEL.toLocaleString()} at a time)`}
          </span>
          <div className="cm-bulk-actions">
            <button type="button" className="cm-btn cm-btn-ghost cm-btn-sm" onClick={() => setSelected(new Set())}>
              Clear selection
            </button>
            <button
              type="button"
              className="cm-btn cm-btn-destructive-outline cm-btn-sm"
              disabled={tooMany}
              onClick={() => {
                setNotice(null);
                setConfirming(liveSelected);
              }}
            >
              Cancel {liveSelected.length.toLocaleString()} selected…
            </button>
          </div>
        </div>
      )}

      {confirming && (
        <CancelConfirm
          ids={confirming}
          onKeep={() => setConfirming(null)}
          onCancelled={(message) => {
            setConfirming(null);
            setSelected(new Set());
            setNotice(message);
            refresh();
          }}
        />
      )}

      <div className="cm-card cm-card-flush">
        {error && !data ? (
          <div className="cm-card-pad cm-stack">
            <p className="cm-error" role="alert">
              {error}
            </p>
            <button type="button" className="cm-btn cm-btn-outline" onClick={refresh}>
              Try again
            </button>
          </div>
        ) : !data ? (
          <div className="cm-card-pad">
            <p className="cm-loading">
              <span className="cm-live-dot" aria-hidden="true" />
              Asking workers what they&rsquo;re holding&hellip;
            </p>
          </div>
        ) : tasks.length === 0 ? (
          <div className="cm-tasks-empty">
            <p className="cm-tasks-empty-title">No delayed tasks</p>
            <p className="cm-tasks-empty-body">
              {data.workers_replied.length === 0
                ? `No worker answered within ${data.timeout_seconds}s, so there is nobody to ask. Delayed tasks are only visible through the worker holding them.`
                : `None of the ${plural(data.workers_replied.length, "worker")} that answered is holding a task with an ETA or countdown.`}
            </p>
          </div>
        ) : (
          <>
            {error && (
              <div className="cm-card-pad">
                <p className="cm-error" role="alert">
                  {error} Showing the last list loaded; retrying every few seconds.
                </p>
              </div>
            )}
            <div className="cm-table-scroll">
              <table className="cm-table cm-table-stack cm-held-table">
                <thead>
                  <tr>
                    {canOperate && (
                      <th scope="col" className="cm-select-cell">
                        <span className="cm-checkbox">
                          <input
                            type="checkbox"
                            checked={allSelected}
                            disabled={selectable.length === 0}
                            onChange={() => setSelected(allSelected ? new Set() : new Set(selectable.map((t) => t.task_id)))}
                            aria-label="Select every delayed task"
                          />
                          <IconCheck />
                        </span>
                      </th>
                    )}
                    <th scope="col">Task</th>
                    <th scope="col" className="cm-col-fit">
                      Runs in
                    </th>
                    <th scope="col">Worker</th>
                    <th scope="col" className="cm-col-fit">
                      Queue
                    </th>
                    <th scope="col">Arguments</th>
                    <th scope="col" className="cm-col-fit">
                      Run
                    </th>
                    {canOperate && (
                      <th scope="col" className="cm-col-fit">
                        <span className="cm-visually-hidden">Actions</span>
                      </th>
                    )}
                  </tr>
                </thead>
                <tbody>
                  {tasks.map((t) => {
                    const etaMs = t.eta ? Date.parse(t.eta) : NaN;
                    const remaining = (etaMs - now) / 1000;
                    return (
                      <tr key={t.task_id}>
                        {canOperate && (
                          <td className="cm-select-cell">
                            {cancellable(t) && (
                              <span className="cm-checkbox">
                                <input
                                  type="checkbox"
                                  checked={selected.has(t.task_id)}
                                  onChange={() => toggle(t.task_id)}
                                  aria-label={`Select ${t.task_name ?? "task"} (${t.task_id})`}
                                />
                                <IconCheck />
                              </span>
                            )}
                          </td>
                        )}
                        <td className="cm-cell-primary">
                          <TaskCell taskId={t.task_id} taskName={t.task_name} />
                        </td>
                        <td className="cm-col-fit" title={formatTime(t.eta)}>
                          <span className="cm-held-cell-label">Runs in</span>
                          {Number.isNaN(etaMs) ? (
                            "—"
                          ) : remaining > 0 ? (
                            <span className="cm-held-countdown">{formatDuration(remaining)}</span>
                          ) : (
                            <span className="cm-held-due">due now</span>
                          )}
                          <span className="cm-held-sub">{formatTime(t.eta)}</span>
                        </td>
                        <td className="cm-held-mono">{t.worker}</td>
                        <td className="cm-col-fit cm-held-mono">
                          {t.queue ?? "—"}
                          {t.priority !== null && t.priority !== undefined && <span className="cm-held-sub">priority {t.priority}</span>}
                        </td>
                        <td>
                          {data.args_revealed ? (
                            <code className="cm-held-args" title={revealedText(t)}>
                              {revealedText(t)}
                            </code>
                          ) : (
                            <code className="cm-held-args cm-held-args-masked" title="Types only. Values are hidden.">
                              {maskedText(t.args_masked)}
                            </code>
                          )}
                        </td>
                        <td className="cm-col-fit">
                          {t.run ? (
                            <>
                              <StatusBadge status={t.run.status} />
                              {t.run.status === "revoked" && <span className="cm-held-sub">dropped at the ETA</span>}
                            </>
                          ) : (
                            <span className="cm-held-sub" title="celery-manager has no run recorded for this task id">
                              not recorded
                            </span>
                          )}
                        </td>
                        {canOperate && (
                          <td className="cm-col-fit">
                            {cancellable(t) && (
                              <button
                                type="button"
                                className="cm-btn cm-btn-outline cm-btn-sm"
                                disabled={!!confirming}
                                onClick={() => {
                                  setNotice(null);
                                  setConfirming([t.task_id]);
                                }}
                              >
                                Cancel…
                              </button>
                            )}
                          </td>
                        )}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>

      {data && data.unheld_runs.length > 0 && (
        <div className="cm-card">
          <div className="cm-card-header">
            <h3 className="cm-card-title">Recorded with a future ETA, but no worker reports holding them</h3>
            <p className="cm-card-description">
              Still waiting on the queue (nothing consumes it yet), held by a worker that didn&rsquo;t answer, or lost. On
              Redis, check the Stuck tab.
              {data.unheld_truncated && " Showing the first 100."}
            </p>
          </div>
          <ul className="cm-held-unheld">
            {data.unheld_runs.map((r) => (
              <li key={r.task_id}>
                <TaskCell taskId={r.task_id} taskName={r.task_name} />
                <span className="cm-held-sub">
                  {formatTime(r.eta)}
                  {r.queue ? ` · ${r.queue}` : ""}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {data && (
        <ul className="cm-held-notes">
          <li>
            Only workers that answer within {data.timeout_seconds}s are asked. {plural(data.workers_replied.length, "worker")}{" "}
            answered.
          </li>
          {!data.args_revealed && <li>Arguments show their types and keyword names; the values stay hidden.</li>}
          {!canOperate && <li>Cancelling needs the operator role. {needsRole("operator")}.</li>}
        </ul>
      )}
    </section>
  );
}

/* ---- Stuck (Redis) --------------------------------------------------------- */

const STATE_LABEL: Record<UnackedState, string> = {
  running: "Running",
  held: "Held by a worker",
  orphaned: "Orphaned",
  unknown: "Unknown",
};

// Reuses the task status colours: running is a running task, held is a
// waiting one, orphaned is the one that needs attention.
const STATE_COLOUR: Record<UnackedState, string> = {
  running: "started",
  held: "queued",
  orphaned: "failed",
  unknown: "skipped",
};

function redeliveryText(entry: UnackedEntry, visibilityTimeout: number, ageNow: number): string {
  if (entry.state === "running") return "Acknowledged when it finishes";
  if (entry.past_visibility_timeout || ageNow >= visibilityTimeout) {
    return entry.state === "orphaned"
      ? "Past the visibility timeout: kombu puts it back at the next worker poll"
      : "Past the visibility timeout: kombu may deliver it again while the worker still holds it";
  }
  const left = formatDuration(visibilityTimeout - ageNow);
  return entry.state === "orphaned" ? `Stuck: kombu redelivers it in ${left}` : `Redelivered by kombu in ${left} if still unacknowledged`;
}

function RestoreConfirm({
  entries,
  onKeep,
  onDone,
}: {
  entries: UnackedEntry[];
  onKeep: () => void;
  onDone: (message: string) => void;
}) {
  const held = entries.filter((e) => e.state === "held");
  const [force, setForce] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const needsForce = held.length > 0;

  async function run() {
    setBusy(true);
    setError(null);
    try {
      const result = await restoreUnacked(
        entries.map((e) => e.tag),
        force,
      );
      const parts = [`Put ${plural(result.restored.length, "message")} back on the queue.`];
      if (result.refused.length > 0) {
        parts.push(`${plural(result.refused.length, "message")} refused: ${result.refused.map((r) => r.reason).join("; ")}.`);
      }
      if (result.missing.length > 0) {
        parts.push(`${plural(result.missing.length, "message")} had already been acknowledged or restored.`);
      }
      onDone(parts.join(" "));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't restore.");
      setBusy(false);
    }
  }

  return (
    <div className="cm-held-confirm" role="group" aria-label="Confirm restoring" onKeyDown={(e) => e.key === "Escape" && onKeep()}>
      <p className="cm-held-confirm-text">
        <strong>
          Put {entries.length === 1 ? "this message" : plural(entries.length, "message")} back at the front of{" "}
          {entries.length === 1 ? "its queue" : "their queues"}?
        </strong>{" "}
        If a task is in fact still running somewhere, it will run twice: a worker that didn&rsquo;t answer in time looks
        exactly like a dead one. A task whose worker died partway through runs again from the start, so only restore
        tasks that are safe to repeat. Anything a live worker reports running is refused.
      </p>
      {needsForce && (
        <label className="cm-held-force">
          <span className="cm-checkbox">
            <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} />
            <IconCheck />
          </span>
          Also restore the {plural(held.length, "message")} a live worker is holding. That worker will still run{" "}
          {held.length === 1 ? "it" : "them"}, so each will run twice.
        </label>
      )}
      <div className="cm-held-confirm-row">
        <button type="button" className="cm-btn cm-btn-outline cm-btn-sm" onClick={onKeep} disabled={busy}>
          Leave {entries.length === 1 ? "it" : "them"}
        </button>
        <button type="button" className="cm-btn cm-btn-destructive cm-btn-sm" onClick={run} disabled={busy} aria-busy={busy}>
          {busy ? "Restoring…" : entries.length === 1 ? "Restore message" : `Restore ${entries.length.toLocaleString()} messages`}
        </button>
      </div>
      {error && (
        <p className="cm-error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

function StuckPanel() {
  const canOperate = useCan("operator");
  const { data, error, refresh, visible } = usePolled<UnackedListing>(listUnacked, STUCK_REFRESH_MS);
  const now = useNow();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirming, setConfirming] = useState<UnackedEntry[] | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const entries = data && data.supported ? data.entries : [];
  // Running ones are never restorable; unknown ones would be refused (the
  // server couldn't ask the workers), so neither gets a checkbox.
  const restorable = (e: UnackedEntry) => e.state === "orphaned" || e.state === "held";
  const selectable = entries.filter(restorable);
  const liveSelected = selectable.filter((e) => selected.has(e.tag));
  const allSelected = selectable.length > 0 && liveSelected.length === selectable.length;
  const tooMany = liveSelected.length > MAX_RESTORE;
  // Ages tick between refreshes, counted from when this listing was read.
  const sinceListing = data ? Math.max(0, (now - Date.parse(data.inspected_at)) / 1000) : 0;

  function toggle(tag: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(tag)) next.delete(tag);
      else next.add(tag);
      return next;
    });
  }

  return (
    <section className="cm-stack" aria-labelledby="cm-held-stuck-heading">
      <div className="cm-held-toolbar">
        <h2 id="cm-held-stuck-heading" className="cm-visually-hidden">
          Stuck tasks
        </h2>
        <p className="cm-held-lede">
          Messages a worker took from a Redis queue but never acknowledged, for example because it crashed.
        </p>
        <div className="cm-held-toolbar-end">
          <LiveIndicator visible={visible} ms={STUCK_REFRESH_MS} />
        </div>
      </div>

      {notice && (
        <p className="cm-notice" role="status">
          {notice}
        </p>
      )}

      {data && data.supported && data.workers_error && (
        <p className="cm-alert" role="status">
          <span>
            Couldn&rsquo;t ask the workers what they hold ({data.workers_error.replace(/\.+$/, "")}), so no entry can be
            classified and restoring is refused until they answer.
          </span>
        </p>
      )}

      {canOperate && liveSelected.length > 0 && !confirming && (
        <div className="cm-bulk-bar" role="group" aria-label="Actions for the selected messages">
          <span className="cm-bulk-bar-label">
            {liveSelected.length.toLocaleString()} selected
            {tooMany && ` (the limit is ${MAX_RESTORE} at a time)`}
          </span>
          <div className="cm-bulk-actions">
            <button type="button" className="cm-btn cm-btn-ghost cm-btn-sm" onClick={() => setSelected(new Set())}>
              Clear selection
            </button>
            <button
              type="button"
              className="cm-btn cm-btn-destructive-outline cm-btn-sm"
              disabled={tooMany}
              onClick={() => {
                setNotice(null);
                setConfirming(liveSelected);
              }}
            >
              Restore {liveSelected.length.toLocaleString()} selected…
            </button>
          </div>
        </div>
      )}

      {confirming && (
        <RestoreConfirm
          entries={confirming}
          onKeep={() => setConfirming(null)}
          onDone={(message) => {
            setConfirming(null);
            setSelected(new Set());
            setNotice(message);
            refresh();
          }}
        />
      )}

      {error && !data ? (
        <div className="cm-card cm-card-pad cm-stack">
          <p className="cm-error" role="alert">
            {error}
          </p>
          <button type="button" className="cm-btn cm-btn-outline" onClick={refresh}>
            Try again
          </button>
        </div>
      ) : !data ? (
        <div className="cm-card cm-card-pad">
          <p className="cm-loading">
            <span className="cm-live-dot" aria-hidden="true" />
            Reading unacknowledged messages&hellip;
          </p>
        </div>
      ) : !data.supported ? (
        <div className="cm-card">
          <div className="cm-card-header">
            <h3 className="cm-card-title">Not available on this broker</h3>
            <p className="cm-card-description">{data.reason}</p>
          </div>
        </div>
      ) : (
        <div className="cm-card cm-card-flush">
          {error && (
            <div className="cm-card-pad">
              <p className="cm-error" role="alert">
                {error} Showing the last list loaded; retrying every few seconds.
              </p>
            </div>
          )}
          {entries.length === 0 ? (
            <div className="cm-tasks-empty">
              <p className="cm-tasks-empty-title">Nothing unacknowledged</p>
              <p className="cm-tasks-empty-body">Every message a worker has taken from Redis has been acknowledged.</p>
            </div>
          ) : (
            <div className="cm-table-scroll">
              <table className="cm-table cm-table-stack cm-held-table">
                <thead>
                  <tr>
                    {canOperate && (
                      <th scope="col" className="cm-select-cell">
                        <span className="cm-checkbox">
                          <input
                            type="checkbox"
                            checked={allSelected}
                            disabled={selectable.length === 0}
                            onChange={() => setSelected(allSelected ? new Set() : new Set(selectable.map((e) => e.tag)))}
                            aria-label="Select every restorable message"
                          />
                          <IconCheck />
                        </span>
                      </th>
                    )}
                    <th scope="col">Task</th>
                    <th scope="col" className="cm-col-fit">
                      Queue
                    </th>
                    <th scope="col" className="cm-col-fit">
                      Held for
                    </th>
                    <th scope="col">State</th>
                    <th scope="col">Redelivery</th>
                    {canOperate && (
                      <th scope="col" className="cm-col-fit">
                        <span className="cm-visually-hidden">Actions</span>
                      </th>
                    )}
                  </tr>
                </thead>
                <tbody>
                  {entries.map((e) => {
                    const ageNow = e.age_seconds + sinceListing;
                    return (
                      <tr key={e.tag}>
                        {canOperate && (
                          <td className="cm-select-cell">
                            {restorable(e) && (
                              <span className="cm-checkbox">
                                <input
                                  type="checkbox"
                                  checked={selected.has(e.tag)}
                                  onChange={() => toggle(e.tag)}
                                  aria-label={`Select ${e.task_name ?? "message"} (${e.task_id ?? e.tag})`}
                                />
                                <IconCheck />
                              </span>
                            )}
                          </td>
                        )}
                        <td className="cm-cell-primary">
                          <TaskCell taskId={e.task_id} taskName={e.message_missing ? "Message missing" : e.task_name} />
                          {e.run_status && <span className="cm-held-sub">recorded as {e.run_status}</span>}
                        </td>
                        <td className="cm-col-fit cm-held-mono">{e.queue ?? "—"}</td>
                        <td className="cm-col-fit" title={`Since ${formatTime(e.held_since)}`}>
                          <span className="cm-held-cell-label">Held for</span>
                          {formatDuration(ageNow)}
                          {e.eta && <span className="cm-held-sub">ETA {formatTime(e.eta)}</span>}
                        </td>
                        <td>
                          <span className="cm-status" data-status={STATE_COLOUR[e.state]}>
                            {STATE_LABEL[e.state]}
                          </span>
                          {e.worker && (
                            <span className="cm-held-sub cm-held-mono">
                              {e.worker_state} on {e.worker}
                            </span>
                          )}
                        </td>
                        <td className="cm-held-redelivery" data-overdue={e.state === "orphaned" && ageNow < data.visibility_timeout_seconds ? "true" : undefined}>
                          {redeliveryText(e, data.visibility_timeout_seconds, ageNow)}
                        </td>
                        {canOperate && (
                          <td className="cm-col-fit">
                            {restorable(e) && (
                              <button
                                type="button"
                                className="cm-btn cm-btn-outline cm-btn-sm"
                                disabled={!!confirming}
                                onClick={() => {
                                  setNotice(null);
                                  setConfirming([e]);
                                }}
                              >
                                Restore…
                              </button>
                            )}
                          </td>
                        )}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}

      {data && data.supported && (
        <ul className="cm-held-notes">
          <li>
            {plural(data.total, "unacknowledged message")}
            {data.total > entries.length ? `; the oldest ${entries.length.toLocaleString()} are shown` : ""}. The visibility
            timeout is {formatDuration(data.visibility_timeout_seconds)} (<code>visibility_timeout</code> in{" "}
            <code>broker_transport_options</code>).
          </li>
          <li>
            <strong>Held</strong> is normal: a worker has the task prefetched or is keeping it until its ETA.{" "}
            <strong>Orphaned</strong> means no worker that answered has it; kombu only redelivers it after the visibility
            timeout, and only while some worker is running.
          </li>
          {data.workers_replied && data.workers_replied.length === 0 && (
            <li>No workers answered, so every entry shows as orphaned. A worker that is busy or slow to answer looks the same.</li>
          )}
          {!canOperate && <li>Restoring needs the operator role. {needsRole("operator")}.</li>}
        </ul>
      )}
    </section>
  );
}

export function HeldTasks() {
  const [params, setParams] = useSearchParams();
  const tab: Tab = params.get("tab") === "stuck" ? "stuck" : "delayed";

  return (
    <div className="cm-page">
      <header className="cm-page-header">
        <div className="cm-page-header-titles">
          <h1 className="cm-page-title">Held tasks</h1>
          <p className="cm-page-lede">Tasks that have left their queue but haven&rsquo;t run yet.</p>
        </div>
      </header>

      <div className="cm-tabs-list" role="group" aria-label="Section">
        <button type="button" className="cm-tabs-trigger" aria-pressed={tab === "delayed"} onClick={() => setParams({ tab: "delayed" }, { replace: true })}>
          Delayed
        </button>
        <button type="button" className="cm-tabs-trigger" aria-pressed={tab === "stuck"} onClick={() => setParams({ tab: "stuck" }, { replace: true })}>
          Stuck (Redis)
        </button>
      </div>

      {tab === "stuck" ? <StuckPanel /> : <DelayedPanel />}
    </div>
  );
}
