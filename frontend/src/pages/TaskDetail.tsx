import "../styles/task-detail.css";

import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { cancelTask, getTask, retryTask, type TaskDetail as TaskDetailData } from "../api/tasks";
import {
  elapsedSeconds,
  formatDuration,
  isRunning,
  progressFraction,
  progressPercent,
  projectedRemainingSeconds,
  secondsSince,
  toMillis,
  useNow,
} from "../lib/taskProgress";

// Statuses where more is still going to happen, so the page keeps polling.
// Anything else — succeeded, failed, skipped, revoked, or a status string
// this build doesn't recognise — is treated as terminal and stops the timer.
// Erring towards "stop" matters more than erring towards "keep watching": a
// stuck interval on a finished run is a bug nobody notices, while a stopped
// one on a live run is one refresh away from fixed.
//
// `retried` is in here despite reading like an ending. Celery emits
// task-retried when a task raises Retry, and the *same* task_id then comes
// back round as task-started for the next attempt (see _STATUS_BY_EVENT in
// events/listener.py, and the retry note in decorator.py about attempts
// reusing the id). Treating it as terminal would freeze the page on a run
// that is about to start working again.
const ACTIVE_STATUSES = new Set(["queued", "started", "retried"]);

// One task row, once every two seconds. Fast enough that a progress bar
// visibly moves, cheap enough that a tab left open on a long import isn't a
// problem. (TaskList polls the whole list, so it uses 5s.)
const POLL_INTERVAL_MS = 2000;

// Statuses a revoke can still mean something for. `received` is not a status
// of its own here: the events listener folds task-received into "queued".
// `retried` is waiting to run again, so it is revocable too.
const CANCELLABLE_STATUSES = new Set(["queued", "started", "retried"]);

// How long a running task can go without reporting progress before the page
// says so. Tasks that report per-item typically update several times a
// second; the throttled ones update every few seconds. A full minute of
// silence is therefore well outside normal cadence for anything instrumented,
// while still being long enough to sit through a slow query or a chunky
// network call without crying wolf.
const STALE_PROGRESS_SECONDS = 60;

function formatPayload(raw: string): string {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

// Same em-dash-for-absent convention TaskList uses, so a task that never
// started reads consistently across both screens rather than showing "null".
function formatTimestamp(value: string | null): string {
  if (!value) return "—";
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

// Clock time, deliberately coarser than formatTimestamp: no seconds. Used for
// the projected finish and the ran-between caption, where second-level
// precision on an extrapolated number would claim an accuracy the estimate
// doesn't have. The date is added only when it isn't today's, so an overnight
// run doesn't silently read as this afternoon.
function formatClock(millis: number): string {
  const at = new Date(millis);
  const time = at.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  if (at.toDateString() === new Date().toDateString()) return time;
  return `${at.toLocaleDateString(undefined, { month: "short", day: "numeric" })} ${time}`;
}

function formatCount(value: number): string {
  return value.toLocaleString();
}

/** A value the system genuinely doesn't have.
 *
 *  The dash is for the eye. Screen readers get the reason instead, because
 *  "dash" announced on its own is a value the listener then has to guess at —
 *  "not recorded" and "still running" are different facts and should sound
 *  different. */
function Absent({ reason }: { reason: string }) {
  return (
    <span className="cm-detail-absent">
      <span aria-hidden="true">—</span>
      <span className="cm-visually-hidden">{reason}</span>
    </span>
  );
}

export function TaskDetail() {
  const { taskId } = useParams<{ taskId: string }>();
  const [task, setTask] = useState<TaskDetailData | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Kept apart from `error` on purpose: a failed *background* refresh must
  // never replace a page full of real data with an error screen. The last
  // known state stays on screen and the failure is reported next to it.
  const [liveError, setLiveError] = useState<string | null>(null);
  const [retrying, setRetrying] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);
  const [retryNotice, setRetryNotice] = useState<{ newTaskId: string } | null>(null);
  // "cancel" | "terminate" while a request is in flight; confirmTerminate is
  // the inline second step that guards the destructive option.
  const [cancelBusy, setCancelBusy] = useState<"cancel" | "terminate" | null>(null);
  const [confirmTerminate, setConfirmTerminate] = useState(false);
  const [cancelError, setCancelError] = useState<string | null>(null);
  const [cancelNotice, setCancelNotice] = useState<string | null>(null);

  useEffect(() => {
    setTask(null);
    setError(null);
    setLiveError(null);
    setRetrying(false);
    setRetryError(null);
    setRetryNotice(null);
    setCancelBusy(null);
    setConfirmTerminate(false);
    setCancelError(null);
    setCancelNotice(null);
    if (!taskId) return;
    // Guarded so a slow response for the *previous* task id can't land after
    // the user has already navigated to another run and overwrite it.
    let cancelled = false;
    getTask(taskId)
      .then((next) => {
        if (!cancelled) setTask(next);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Task not found.");
      });
    return () => {
      cancelled = true;
    };
  }, [taskId]);

  const running = task !== null && isRunning(task);
  const live = task !== null && ACTIVE_STATUSES.has(task.status);

  // Live refresh: a plain poll, not the SSE helper. /api/stream reads an
  // in-process EventBus, but the documented deployment runs the events
  // listener as a separate OS process that cannot publish into it — so SSE
  // connects and then delivers nothing, which is exactly why TaskList polls
  // too. A poll is the path that actually works.
  //
  // `live` is what stops it: the moment a poll returns a terminal status,
  // this effect's dependency flips to false, React runs the cleanup, and the
  // interval is cleared. Nothing keeps ticking on a finished run.
  useEffect(() => {
    if (!taskId || !live) return;
    // `cancelled` as well as clearInterval, because clearing the timer does
    // nothing about the request it already dispatched. Navigating from one
    // live run to another tears this effect down while a getTask() for the
    // *old* id is still in flight; without the guard its response lands
    // afterwards and repaints the new run's page with the old run's data
    // until the new fetch overtakes it. Same reason the loader effect above
    // carries one.
    let cancelled = false;
    const timer = setInterval(() => {
      getTask(taskId)
        .then((next) => {
          if (cancelled) return;
          setTask(next);
          setLiveError(null);
        })
        .catch((err) => {
          if (cancelled) return;
          setLiveError(err instanceof Error ? err.message : "Couldn't refresh this run.");
        });
    }, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [taskId, live]);

  // The one-second clock behind the elapsed counter. Only runs while the task
  // is actually running — a finished run's page settles completely instead of
  // re-rendering forever behind a background tab.
  const now = useNow(running);

  const refreshTask = useCallback(async (id: string) => {
    try {
      setTask(await getTask(id));
    } catch {
      // The retry itself already succeeded; failing to re-read the run
      // afterwards is not a retry failure and must not be reported as one.
    }
  }, []);

  async function handleRetry() {
    if (!task) return;
    setRetrying(true);
    setRetryError(null);
    setRetryNotice(null);
    try {
      const { new_task_id } = await retryTask(task.task_id);
      setRetryNotice({ newTaskId: new_task_id });
      // A failed run is terminal, so nothing is polling it. Without this
      // re-read, the retry we just made would be missing from the lineage
      // panel until the operator reloaded the page by hand.
      await refreshTask(task.task_id);
    } catch (err) {
      setRetryError(err instanceof Error ? err.message : "Retry failed.");
    } finally {
      setRetrying(false);
    }
  }

  async function handleCancel(terminate: boolean) {
    if (!task) return;
    setCancelBusy(terminate ? "terminate" : "cancel");
    setCancelError(null);
    setCancelNotice(null);
    try {
      await cancelTask(task.task_id, terminate);
      setConfirmTerminate(false);
      // Worded as a request on purpose: revoke is fire-and-forget. A queued
      // task is only skipped when a worker reaches it, and the status does
      // not change until the worker reports back.
      setCancelNotice(
        terminate
          ? "Terminate requested — the worker will be asked to kill this run."
          : task.status === "started"
            ? "Cancel requested. A run already in progress keeps going; use Terminate to stop it."
            : "Cancel requested — it takes effect when a worker reaches this task."
      );
      await refreshTask(task.task_id);
    } catch (err) {
      setCancelError(err instanceof Error ? err.message : "Cancel failed.");
    } finally {
      setCancelBusy(null);
    }
  }

  const canCancel = task !== null && CANCELLABLE_STATUSES.has(task.status);
  const canTerminate = task !== null && task.status === "started";

  const fraction = task ? progressFraction(task) : null;
  const elapsed = task ? elapsedSeconds(task, now) : null;
  const remaining = task ? projectedRemainingSeconds(task, elapsed) : null;
  // Only meaningful while the run is live: a finished task's last progress
  // update is supposed to be old.
  const sinceProgress = task && running ? secondsSince(task.progress_updated_at, now) : null;
  const stale = sinceProgress !== null && sinceProgress >= STALE_PROGRESS_SECONDS;
  // Suppressed while stale rather than shown alongside the warning: an
  // estimate extrapolated from a rate that has stopped is worse than no
  // estimate, because it keeps counting down as if nothing were wrong.
  const showEstimate = remaining !== null && !stale;

  // Never render 100% on a run that is still going: rounding 0.998 up reads
  // as a finished task that is somehow still working. That rule now lives in
  // lib/taskProgress alongside every other derivation, because the task list
  // rounded plainly and the two screens quoted different percentages for the
  // same run in the last half-percent.
  const percent = task ? progressPercent(task) : null;

  // Why a timestamp is missing depends on where the run is: a queued task
  // hasn't started, a running one hasn't finished, and anything else simply
  // never had the value recorded. Screen readers get this string in place of
  // the em dash, so it has to be true for all three.
  const pendingReason = !task ? "Not recorded" : running ? "Still running" : task.status === "queued" ? "Hasn't started yet" : "Not recorded";

  const startedMillis = task ? toMillis(task.started_at) : null;
  const finishedMillis = task ? toMillis(task.finished_at) : null;
  const hasLineage = task !== null && (task.retried_from_id !== null || task.retried_by.length > 0);

  return (
    <div className="cm-page cm-detail-page">
      <Link to="/" className="cm-back-link">
        &larr; All tasks
      </Link>

      {error ? (
        <p className="cm-error" role="alert">
          {error}
        </p>
      ) : !task ? (
        <p className="cm-loading">
          <span className="cm-live-dot" aria-hidden="true" />
          Loading&hellip;
        </p>
      ) : (
        <>
          <header className="cm-detail-header">
            <h1 className="cm-page-title">{task.task_name}</h1>
            <span className="cm-status-badge" data-status={task.status}>
              {task.status}
            </span>
            {task.status === "failed" && (
              <button
                type="button"
                className="cm-btn cm-btn-solid"
                onClick={handleRetry}
                disabled={retrying}
                aria-busy={retrying}
              >
                {retrying ? "Retrying…" : "Retry"}
              </button>
            )}
          </header>
          <p className="cm-task-id cm-detail-task-id">{task.task_id}</p>

          {canCancel && (
            <div className="cm-task-actions" role="group" aria-label="Task actions">
              {confirmTerminate && canTerminate ? (
                <>
                  <span className="cm-task-actions-warn">Kills the worker process running this task. Unsaved work is lost.</span>
                  <button
                    type="button"
                    className="cm-btn cm-btn-danger"
                    onClick={() => handleCancel(true)}
                    disabled={cancelBusy !== null}
                    aria-busy={cancelBusy === "terminate"}
                  >
                    {cancelBusy === "terminate" ? "Terminating…" : "Confirm terminate"}
                  </button>
                  <button
                    type="button"
                    className="cm-btn cm-btn-outline"
                    onClick={() => setConfirmTerminate(false)}
                    disabled={cancelBusy !== null}
                  >
                    Keep running
                  </button>
                </>
              ) : (
                <>
                  <button
                    type="button"
                    className="cm-btn cm-btn-outline"
                    onClick={() => handleCancel(false)}
                    disabled={cancelBusy !== null}
                    aria-busy={cancelBusy === "cancel"}
                  >
                    {cancelBusy === "cancel" ? "Cancelling…" : "Cancel"}
                  </button>
                  {canTerminate && (
                    <button
                      type="button"
                      className="cm-btn cm-btn-outline cm-btn-danger-outline"
                      onClick={() => setConfirmTerminate(true)}
                      disabled={cancelBusy !== null}
                    >
                      Terminate
                    </button>
                  )}
                </>
              )}
            </div>
          )}

          {cancelNotice && (
            <p className="cm-notice" role="status">
              {cancelNotice}
            </p>
          )}
          {cancelError && (
            <p className="cm-error" role="alert">
              {cancelError}
            </p>
          )}

          {retryNotice && (
            <p className="cm-notice" role="status">
              Retried as a new run &mdash;{" "}
              <Link to={`/tasks/${retryNotice.newTaskId}`} className="cm-back-link">
                view {retryNotice.newTaskId}
              </Link>
              .
            </p>
          )}
          {retryError && (
            <p className="cm-error" role="alert">
              {retryError}
            </p>
          )}
          {liveError && (
            <p className="cm-run-alert" role="status">
              Live updates paused. {liveError} Still retrying every {POLL_INTERVAL_MS / 1000}s.
            </p>
          )}

          {/* Panel order is "what does someone debugging at 2am read first".
              The failure only renders when there is one, so on a running task
              the run panel is effectively the top of the page — and on a
              failed one the exception is, ahead of every timing. */}
          {task.exception && (
            <section className="cm-panel cm-failure-panel">
              <h2>Why it failed</h2>
              <p className="cm-failure-message">{task.exception}</p>
              {task.traceback && <pre className="cm-traceback">{task.traceback}</pre>}
            </section>
          )}

          <section className="cm-panel cm-run-panel" aria-labelledby="cm-run-heading">
            <div className="cm-run-head">
              <h2 id="cm-run-heading">Run</h2>
              {live && (
                <span className="cm-live-indicator" title={`Refreshing every ${POLL_INTERVAL_MS / 1000}s while this run is active`}>
                  <span className="cm-live-dot" aria-hidden="true" />
                  Live
                </span>
              )}
            </div>

            {task.status === "queued" ? (
              <p className="cm-empty-state cm-run-hint">
                Queued &mdash; waiting for a worker to pick this up.
                {task.eta ? ` Not before ${formatTimestamp(task.eta)}.` : ""}
              </p>
            ) : running ? (
              <>
                {fraction !== null && percent !== null && (
                  <div className="cm-run-progress">
                    {/* aria-hidden for the same reason .cm-run-message below
                        is: the progressbar's aria-valuetext already says
                        "21% — 4,182 of 20,000", and without this the tree
                        reads "21% 4,182 of 20,000" and then immediately
                        "Run progress, 21% — 4,182 of 20,000 — …". Verified
                        against a real accessibility tree, not inferred. Safe
                        because this head only ever renders in the branch
                        that also renders the bar; the open-ended branch
                        below has no bar, so its head stays announced. */}
                    <div className="cm-run-progress-head" aria-hidden="true">
                      <span className="cm-run-pct">
                        {percent}
                        <span className="cm-run-pct-unit">%</span>
                      </span>
                      <span className="cm-run-count">
                        {formatCount(task.progress_current ?? 0)} of {formatCount(task.progress_total ?? 0)}
                      </span>
                    </div>
                    <div
                      className="cm-run-track"
                      // The list drains its run lane when a run stops
                      // reporting; without the same flag here the same run
                      // looked healthy on its own page and stalled in the
                      // table one click away.
                      data-stalled={stale ? "true" : undefined}
                      role="progressbar"
                      aria-label="Run progress"
                      aria-valuemin={0}
                      aria-valuemax={100}
                      aria-valuenow={percent}
                      aria-valuetext={`${percent}% — ${formatCount(task.progress_current ?? 0)} of ${formatCount(
                        task.progress_total ?? 0
                      )}${task.progress_message ? ` — ${task.progress_message}` : ""}`}
                    >
                      <div className="cm-run-fill" style={{ width: `${percent}%` }} />
                    </div>
                    {/* aria-hidden because the progressbar's aria-valuetext
                        above already ends with this same message; without it a
                        screen reader reads it twice in a row. The open-ended
                        branch below has no progressbar, so its copy of this
                        line stays announced — it is the only announcement
                        there is. */}
                    {task.progress_message && (
                      <p className="cm-run-message" aria-hidden="true">
                        {task.progress_message}
                      </p>
                    )}
                  </div>
                )}

                {/* Open-ended progress: a count with no denominator. No bar
                    and no percentage, because there is no percentage to
                    compute — the count itself becomes the headline figure,
                    and the caption says why there's nothing to fill. */}
                {fraction === null && task.progress_current !== null && (
                  <div className="cm-run-progress">
                    <div className="cm-run-progress-head">
                      <span className="cm-run-pct">{formatCount(task.progress_current)}</span>
                      <span className="cm-run-count">no total reported</span>
                    </div>
                    {task.progress_message && <p className="cm-run-message">{task.progress_message}</p>}
                  </div>
                )}

                {fraction !== null ? (
                  <dl className="cm-detail-meta cm-run-stats">
                    <div>
                      <dt>Elapsed</dt>
                      <dd>{elapsed === null ? <Absent reason="Not recorded" /> : formatDuration(elapsed)}</dd>
                    </div>
                    {showEstimate && remaining !== null && (
                      <>
                        <div>
                          <dt>Est. remaining</dt>
                          <dd>≈ {formatDuration(remaining)}</dd>
                        </div>
                        <div>
                          <dt>Est. finish</dt>
                          <dd>≈ {formatClock(now + remaining * 1000)}</dd>
                        </div>
                      </>
                    )}
                  </dl>
                ) : elapsed === null ? (
                  <p className="cm-empty-state cm-run-hint">
                    No elapsed time to show &mdash; this run has no recorded start time.
                  </p>
                ) : (
                  <p className="cm-run-hero">
                    <span className="cm-run-hero-label">Elapsed</span>
                    <span className="cm-run-hero-value">{formatDuration(elapsed)}</span>
                  </p>
                )}

                {/* Both estimate figures are marked "Est." and prefixed with
                    ≈, and this line says what the estimate is actually made
                    of. A countdown answers "do I wait?"; the clock time
                    answers "will it be done before the 15:00 deploy" — they
                    aren't the same question, so both are shown. */}
                {fraction !== null && showEstimate && (
                  <p className="cm-run-estimate-note">
                    Estimated by projecting the average rate so far. Work with uneven cost per item will move it.
                  </p>
                )}
                {fraction !== null && !showEstimate && !stale && (
                  <p className="cm-run-estimate-note">
                    No finish estimate yet &mdash; there isn't enough progress to extrapolate a rate from.
                  </p>
                )}

                {/* A wedged run and a fast one look identical without this. */}
                {stale && sinceProgress !== null && (
                  <p className="cm-run-alert" role="status">
                    No progress update in {formatDuration(sinceProgress)} &mdash; this run may be wedged. Estimates are
                    paused until it reports again.
                  </p>
                )}

                {fraction === null && task.progress_current === null && (
                  <p className="cm-empty-state cm-run-hint">
                    This task doesn't report progress, so there's nothing to fill in. Call{" "}
                    <code className="cm-run-code">celery_manager.report_progress(current, total, message)</code>{" "}
                    inside the task body and a live bar appears here on the next run.
                  </p>
                )}
              </>
            ) : elapsed !== null ? (
              <>
                <p className="cm-run-hero">
                  <span className="cm-run-hero-label">Duration</span>
                  <span className="cm-run-hero-value">{formatDuration(elapsed)}</span>
                </p>
                {startedMillis !== null && finishedMillis !== null && (
                  <p className="cm-run-window">
                    <time dateTime={task.started_at ?? undefined}>{formatClock(startedMillis)}</time>
                    {" → "}
                    <time dateTime={task.finished_at ?? undefined}>{formatClock(finishedMillis)}</time>
                  </p>
                )}
                {task.progress_current !== null && (
                  <p className="cm-run-message">
                    Last reported {formatCount(task.progress_current)}
                    {task.progress_total !== null ? ` of ${formatCount(task.progress_total)}` : ""}
                    {task.progress_message ? ` — ${task.progress_message}` : ""}
                  </p>
                )}
              </>
            ) : (
              <p className="cm-empty-state cm-run-hint">
                No duration recorded &mdash; this run never reported a start time.
              </p>
            )}
          </section>

          {/* Where it ran. Second because "which box, off which queue" is the
              next question after "what broke", and it's what you correlate
              against infra graphs. Every value falls back to a dash with a
              spoken reason, so a queued-but-never-started run renders a
              complete row set rather than a ragged one. */}
          <section className="cm-panel cm-exec-panel" aria-labelledby="cm-exec-heading">
            <h2 id="cm-exec-heading">Execution</h2>
            <dl className="cm-detail-meta">
              <div>
                <dt>Worker</dt>
                <dd>{task.worker ?? <Absent reason="Not recorded" />}</dd>
              </div>
              <div>
                <dt>Queue</dt>
                <dd>{task.queue ?? <Absent reason="Not recorded" />}</dd>
              </div>
              <div>
                <dt>ETA</dt>
                <dd>{task.eta ? formatTimestamp(task.eta) : <Absent reason="No ETA set" />}</dd>
              </div>
              <div>
                <dt>Started</dt>
                <dd>{task.started_at ? formatTimestamp(task.started_at) : <Absent reason="Hasn't started yet" />}</dd>
              </div>
              <div>
                <dt>Finished</dt>
                <dd>
                  {task.finished_at ? (
                    formatTimestamp(task.finished_at)
                  ) : (
                    <Absent reason={pendingReason} />
                  )}
                </dd>
              </div>
              <div>
                <dt>Runtime</dt>
                <dd>
                  {task.runtime_seconds !== null ? (
                    formatDuration(task.runtime_seconds)
                  ) : (
                    <Absent reason={pendingReason} />
                  )}
                </dd>
              </div>
            </dl>
          </section>

          {/* Omitted entirely when there's no chain — an empty "Retry
              lineage" panel would be a row of dashes saying nothing. */}
          {hasLineage && (
            <section className="cm-panel cm-lineage-panel" aria-labelledby="cm-lineage-heading">
              <h2 id="cm-lineage-heading">Retry lineage</h2>
              <ol className="cm-lineage-chain">
                {task.retried_from_id && (
                  <li className="cm-lineage-item" data-node="from">
                    <span className="cm-lineage-label">Retried from</span>
                    <Link to={`/tasks/${task.retried_from_id}`} className="cm-lineage-link">
                      {task.retried_from_id}
                    </Link>
                  </li>
                )}
                <li className="cm-lineage-item" data-node="self" aria-current="page">
                  <span className="cm-lineage-label">This run</span>
                  <span className="cm-lineage-id">{task.task_id}</span>
                </li>
                {/* Numbered only when there is more than one, because then
                    the number carries real information: the order an
                    operator retried this same failed run in. */}
                {task.retried_by.map((id, index) => (
                  <li key={id} className="cm-lineage-item" data-node="to">
                    <span className="cm-lineage-label">
                      {task.retried_by.length > 1 ? `Retry ${index + 1}` : "Retried as"}
                    </span>
                    <Link to={`/tasks/${id}`} className="cm-lineage-link">
                      {id}
                    </Link>
                  </li>
                ))}
              </ol>
            </section>
          )}

          {/* Logs before args: the log is what you read next after the
              traceback, while args/kwargs are the reproduction recipe you
              copy out once you already know what went wrong. */}
          <section className="cm-panel cm-log-panel">
            <h2>Log output</h2>
            {task.log_output ? (
              <pre className="cm-log-output">{task.log_output}</pre>
            ) : (
              <p className="cm-empty-state">No log output captured for this run.</p>
            )}
          </section>

          <dl className="cm-detail-meta">
            <div>
              <dt>Args</dt>
              <dd>
                <pre className="cm-code-block">{formatPayload(task.args)}</pre>
              </dd>
            </div>
            <div>
              <dt>Kwargs</dt>
              <dd>
                <pre className="cm-code-block">{formatPayload(task.kwargs)}</pre>
              </dd>
            </div>
          </dl>
        </>
      )}
    </div>
  );
}
