import { useCallback, useEffect, useId, useRef, useState } from "react";
import type { FormEvent } from "react";
import { Link, useParams } from "react-router-dom";
import {
  deleteQueueMessage,
  listQueueMessages,
  listQueues,
  moveQueueMessage,
  type QueuedMessage,
  type QueueMessages as QueueMessagesPage,
} from "../api/queues";
import { Modal } from "../components/Modal";
import { StatusBadge } from "../components/Status";
import { IconChevronLeft } from "../components/icons";
import { needsRole, useCan } from "../lib/role";
import "../styles/queues.css";
import "../styles/tasks.css";
import "../styles/queue-messages.css";

const PAGE_SIZE = 50;

/** "45s", "12m", "3h 5m", "2d 4h": how long a message has waited. */
function formatAge(seconds: number | null): string {
  if (seconds === null) return "—";
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return minutes % 60 ? `${hours}h ${minutes % 60}m` : `${hours}h`;
  const days = Math.floor(hours / 24);
  return hours % 24 ? `${days}d ${hours % 24}h` : `${days}d`;
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatEta(value: string | null): string | null {
  if (!value) return null;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function callText(message: QueuedMessage): string {
  const parts = [message.args, message.kwargs && message.kwargs !== "{}" ? message.kwargs : null];
  return parts.filter(Boolean).join(" ");
}

function TaskCell({ message }: { message: QueuedMessage }) {
  const name = <span className="cm-task-name">{message.task_name ?? "Unknown task"}</span>;
  if (!message.task_id) return name;
  // Linked only when celery-manager has a run for it; otherwise the run page
  // would be a 404 and the id is shown as plain text to copy.
  return message.run ? (
    <Link to={`/tasks/${message.task_id}`} className="cm-task-link" title={message.task_id}>
      {name}
      <span className="cm-task-id">{message.task_id.slice(0, 8)}</span>
    </Link>
  ) : (
    <span className="cm-task-link" title={`${message.task_id} (no recorded run)`}>
      {name}
      <span className="cm-task-id">{message.task_id.slice(0, 8)}</span>
    </span>
  );
}

function DeleteDialog({
  queue,
  message,
  onClose,
  onDone,
}: {
  queue: string;
  message: QueuedMessage;
  onClose: () => void;
  onDone: (notice: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const taskId = message.task_id as string;

  async function submit() {
    setBusy(true);
    setError(null);
    try {
      await deleteQueueMessage(queue, taskId);
      onDone(`Deleted ${message.task_name ?? "the message"} (${taskId.slice(0, 8)}) from ${queue}.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't delete the message.");
      setBusy(false);
    }
  }

  return (
    <Modal
      title="Delete this message?"
      description="The change is recorded in the audit log."
      busy={busy}
      onClose={onClose}
    >
      <div className="cm-modal-body">
        <p>
          <strong>{message.task_name ?? "This task"}</strong> (<code>{taskId}</code>) will be removed from{" "}
          <strong>{queue}</strong> and will never run. This can&rsquo;t be undone. If a worker takes it first, nothing
          is deleted.
        </p>
        {error && (
          <p className="cm-error" role="alert">
            {error}
          </p>
        )}
        <div className="cm-modal-actions">
          <button type="button" className="cm-btn cm-btn-outline" onClick={onClose} disabled={busy}>
            Keep it
          </button>
          <button type="button" className="cm-btn cm-btn-destructive" onClick={submit} disabled={busy} aria-busy={busy}>
            {busy ? "Deleting…" : "Delete message"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

function MoveDialog({
  queue,
  message,
  onClose,
  onDone,
}: {
  queue: string;
  message: QueuedMessage;
  onClose: () => void;
  onDone: (notice: string) => void;
}) {
  const uid = useId();
  const [targets, setTargets] = useState<string[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [target, setTarget] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const taskId = message.task_id as string;

  useEffect(() => {
    let cancelled = false;
    // The queues the server accepts as a target are the ones the Queues page
    // lists (configured, routed, seen on a run, consumed by a worker).
    listQueues()
      .then((listing) => {
        if (cancelled) return;
        const names = listing.queues.map((q) => q.name).filter((name) => name !== queue);
        setTargets(names);
        setTarget(names[0] ?? "");
      })
      .catch((err) => {
        if (!cancelled) setLoadError(err instanceof Error ? err.message : "Couldn't load the queues.");
      });
    return () => {
      cancelled = true;
    };
  }, [queue]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!target) return;
    setBusy(true);
    setError(null);
    try {
      await moveQueueMessage(queue, taskId, target);
      onDone(`Moved ${message.task_name ?? "the message"} (${taskId.slice(0, 8)}) to ${target}.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't move the message.");
      setBusy(false);
    }
  }

  return (
    <Modal
      title="Move this message"
      description="It goes to the end of the other queue, keeping its priority. The change is recorded in the audit log."
      busy={busy}
      onClose={onClose}
    >
      <form className="cm-modal-body" onSubmit={submit}>
        <p>
          <strong>{message.task_name ?? "This task"}</strong> (<code>{taskId}</code>), now in <strong>{queue}</strong>.
          It will run on whichever workers consume the queue you pick.
        </p>
        {loadError ? (
          <p className="cm-error" role="alert">
            {loadError}
          </p>
        ) : targets === null ? (
          <p className="cm-loading">
            <span className="cm-live-dot" aria-hidden="true" />
            Loading queues&hellip;
          </p>
        ) : targets.length === 0 ? (
          <p className="cm-queue-unknown">There is no other known queue to move it to.</p>
        ) : (
          <div className="cm-field">
            <label htmlFor={`${uid}-target`}>Move to</label>
            <select
              id={`${uid}-target`}
              className="cm-select"
              value={target}
              onChange={(e) => setTarget(e.target.value)}
              disabled={busy}
            >
              {targets.map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </select>
          </div>
        )}
        {error && (
          <p className="cm-error" role="alert">
            {error}
          </p>
        )}
        <div className="cm-modal-actions">
          <button type="button" className="cm-btn cm-btn-outline" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="submit" className="cm-btn cm-btn-primary" disabled={busy || !target} aria-busy={busy}>
            {busy ? "Moving…" : "Move message"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

export function QueueMessages() {
  const { name = "" } = useParams<{ name: string }>();
  // Deleting or moving a message decides whether and where a task runs, like
  // a purge: admin only (server-enforced; this just hides the buttons).
  const canAdmin = useCan("admin");
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<QueueMessagesPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [acting, setActing] = useState<{ kind: "delete" | "move"; message: QueuedMessage } | null>(null);
  const requestIdRef = useRef(0);

  // No auto-refresh: on RabbitMQ every look takes messages off the queue and
  // puts them back, so it should only happen when someone asks.
  const load = useCallback(async () => {
    const requestId = ++requestIdRef.current;
    setLoading(true);
    try {
      const next = await listQueueMessages(name, offset, PAGE_SIZE);
      if (requestId !== requestIdRef.current) return;
      // A page past the end (its messages ran meanwhile) steps back.
      if (next.messages.length === 0 && offset > 0 && (next.total ?? 0) > 0) {
        setOffset(Math.max(0, Math.floor(((next.total ?? 1) - 1) / PAGE_SIZE) * PAGE_SIZE));
        return;
      }
      setPage(next);
      setError(null);
    } catch (err) {
      if (requestId !== requestIdRef.current) return;
      setError(err instanceof Error ? err.message : "Couldn't load the messages in this queue.");
    } finally {
      if (requestId === requestIdRef.current) setLoading(false);
    }
  }, [name, offset]);

  useEffect(() => {
    load();
  }, [load]);

  const messages = page?.messages ?? [];
  const total = page?.total ?? 0;
  const pageSize = page?.approximate ? page.limit : PAGE_SIZE;
  // Single-message actions need Redis; the server refuses them elsewhere.
  const actionsAvailable = canAdmin && page?.transport === "redis" && page.supported;

  return (
    <div className="cm-page">
      <Link to="/queues" className="cm-btn cm-btn-ghost cm-btn-sm cm-back-link">
        <IconChevronLeft />
        All queues
      </Link>

      <header className="cm-page-header">
        <div className="cm-page-header-titles">
          <h1 className="cm-page-title cm-queue-msgs-title">{name}</h1>
          <p className="cm-page-lede">
            Messages waiting in this queue, next to run first.
            {page && !page.approximate && page.supported && " Looking doesn't take them off the queue."}
          </p>
        </div>
        <div className="cm-header-actions">
          <button type="button" className="cm-btn cm-btn-outline" onClick={load} disabled={loading} aria-busy={loading}>
            {loading && page ? "Refreshing…" : "Refresh"}
          </button>
        </div>
      </header>

      {page?.approximate && (
        <div className="cm-alert cm-queue-msgs-approx" role="status">
          <span>
            <strong>Approximate.</strong> RabbitMQ can only show a message by handing it out and taking it back, so
            messages you look at are marked redelivered and may move to the front of the queue. At most{" "}
            {page.limit} are shown.
          </span>
        </div>
      )}
      {page && !page.args_visible && (
        <p className="cm-notice" role="status">
          Task arguments are hidden on this dashboard (<code>queue_browser_show_args</code> is off).
        </p>
      )}
      {notice && (
        <p className="cm-notice" role="status">
          {notice}
        </p>
      )}

      <div className="cm-card cm-card-flush">
        {error && !page ? (
          <div className="cm-card-pad cm-stack">
            <p className="cm-error" role="alert">
              {error}
            </p>
            <button type="button" className="cm-btn cm-btn-outline" onClick={load}>
              Try again
            </button>
          </div>
        ) : !page ? (
          <div className="cm-card-pad">
            <p className="cm-loading">
              <span className="cm-live-dot" aria-hidden="true" />
              Loading messages&hellip;
            </p>
          </div>
        ) : !page.supported ? (
          <div className="cm-tasks-empty">
            <p className="cm-tasks-empty-title">Messages can&rsquo;t be shown for this queue</p>
            <p className="cm-tasks-empty-body">{page.reason}</p>
          </div>
        ) : messages.length === 0 ? (
          <div className="cm-tasks-empty">
            <p className="cm-tasks-empty-title">Nothing waiting</p>
            <p className="cm-tasks-empty-body">
              No messages are waiting in {name}. Tasks a worker has already taken, including ones held for an ETA, are
              not in the queue any more.
            </p>
          </div>
        ) : (
          <>
            {error && (
              <div className="cm-card-pad">
                <p className="cm-error" role="alert">
                  {error} Showing the last messages loaded.
                </p>
              </div>
            )}
            <div className="cm-table-scroll" aria-busy={loading}>
              <table className="cm-table cm-table-stack cm-queue-msgs-table">
                <thead>
                  <tr>
                    <th scope="col" className="cm-col-fit cm-num" title="Order workers will take them in">
                      #
                    </th>
                    <th scope="col">Task</th>
                    <th scope="col">Arguments</th>
                    <th scope="col" className="cm-col-fit cm-num">
                      Priority
                    </th>
                    <th scope="col" className="cm-col-fit cm-num" title="How long it has waited, when known">
                      Age
                    </th>
                    <th scope="col" className="cm-col-fit cm-num">
                      Size
                    </th>
                    {actionsAvailable && (
                      <th scope="col" className="cm-col-fit">
                        <span className="cm-visually-hidden">Actions</span>
                      </th>
                    )}
                  </tr>
                </thead>
                <tbody>
                  {messages.map((m) => {
                    const call = callText(m);
                    const eta = formatEta(m.eta);
                    return (
                      <tr key={`${m.position}-${m.task_id ?? ""}`}>
                        <td className="cm-col-fit cm-num cm-queue-msgs-pos">
                          <span className="cm-queue-cell-label">Position</span>
                          {(m.position + 1).toLocaleString()}
                        </td>
                        <td className="cm-cell-primary">
                          <TaskCell message={m} />
                          <span className="cm-queue-msgs-meta">
                            {m.run && <StatusBadge status={m.run.status} />}
                            {eta && <span>ETA {eta}</span>}
                            {m.retries ? <span>retry {m.retries}</span> : null}
                          </span>
                        </td>
                        <td className="cm-queue-msgs-args">
                          {call ? (
                            <code title={call}>{call}</code>
                          ) : (
                            <span className="cm-queue-unknown">
                              {!page.args_visible ? "hidden" : (m.args_note ?? "—")}
                            </span>
                          )}
                        </td>
                        <td className="cm-col-fit cm-num">
                          <span className="cm-queue-cell-label">Priority</span>
                          {m.priority ?? "—"}
                        </td>
                        <td className="cm-col-fit cm-num" title={m.sent_at ?? "Celery doesn't record when this was sent"}>
                          <span className="cm-queue-cell-label">Age</span>
                          {formatAge(m.age_seconds)}
                        </td>
                        <td className="cm-col-fit cm-num">
                          <span className="cm-queue-cell-label">Size</span>
                          {formatSize(m.size_bytes)}
                        </td>
                        {actionsAvailable && (
                          <td className="cm-col-fit cm-queue-actions">
                            {m.task_id && (
                              <div className="cm-queue-msgs-actions">
                                <button
                                  type="button"
                                  className="cm-btn cm-btn-outline cm-btn-sm"
                                  onClick={() => {
                                    setNotice(null);
                                    setActing({ kind: "move", message: m });
                                  }}
                                >
                                  Move…
                                </button>
                                <button
                                  type="button"
                                  className="cm-btn cm-btn-destructive-outline cm-btn-sm"
                                  onClick={() => {
                                    setNotice(null);
                                    setActing({ kind: "delete", message: m });
                                  }}
                                >
                                  Delete…
                                </button>
                              </div>
                            )}
                          </td>
                        )}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            <div className="cm-tasks-foot">
              <span className="cm-tasks-count">
                {`${(offset + 1).toLocaleString()}–${(offset + messages.length).toLocaleString()} of ${total.toLocaleString()} waiting`}
                {!canAdmin && page.transport === "redis" && (
                  <span title={needsRole("admin")}> &middot; deleting or moving a message needs the admin role</span>
                )}
              </span>
              {!page.approximate && total > pageSize && (
                <div className="cm-tasks-pager">
                  <button
                    type="button"
                    className="cm-btn cm-btn-outline cm-btn-sm"
                    onClick={() => setOffset(Math.max(0, offset - pageSize))}
                    disabled={offset === 0 || loading}
                  >
                    Sooner
                  </button>
                  <span className="cm-tasks-pager-position">
                    Page {(Math.floor(offset / pageSize) + 1).toLocaleString()} of{" "}
                    {Math.ceil(total / pageSize).toLocaleString()}
                  </span>
                  <button
                    type="button"
                    className="cm-btn cm-btn-outline cm-btn-sm"
                    onClick={() => setOffset(offset + pageSize)}
                    disabled={offset + pageSize >= total || loading}
                  >
                    Later
                  </button>
                </div>
              )}
            </div>
          </>
        )}
      </div>

      {acting?.kind === "delete" && (
        <DeleteDialog
          queue={name}
          message={acting.message}
          onClose={() => setActing(null)}
          onDone={(text) => {
            setActing(null);
            setNotice(text);
            load();
          }}
        />
      )}
      {acting?.kind === "move" && (
        <MoveDialog
          queue={name}
          message={acting.message}
          onClose={() => setActing(null)}
          onDone={(text) => {
            setActing(null);
            setNotice(text);
            load();
          }}
        />
      )}
    </div>
  );
}
