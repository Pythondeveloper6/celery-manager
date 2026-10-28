import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  browseDeadLetterQueue,
  listDeadLetterQueues,
  requeueDeadLetters,
  type DeadLetterMessage,
  type DeadLetterMessages,
  type DeadLetterQueueListing,
  type NotConfigured,
  type RequeueResult,
} from "../api/rabbitmq";
import { IconCheck, IconChevronLeft } from "../components/icons";
import { Modal } from "../components/Modal";
import { useCan } from "../lib/role";
import "../styles/queues.css";
import "../styles/rabbitmq.css";

/* ==========================================================================
   RabbitMQ dead-letter queues: what RabbitMQ dead-lettered (rejected,
   expired, over length), and sending it back where it came from.

   Reading a DLQ is approximate: RabbitMQ can only show a message by handing
   it out and taking it back, so a look marks messages redelivered and may
   reorder them. Requeue is admin-only and audited; the server publishes
   each copy with confirms before removing the original, so a failure never
   loses a message (see docs/rabbitmq.md).
   ========================================================================== */

const DOCS_URL = "https://pythondeveloper6.github.io/celery-manager/rabbitmq/";
const ALL_LIMIT = 100;

const MATCH_LABEL: Record<string, string> = {
  dead_letter_target: "dead-letter target",
  name_pattern: "name pattern",
};

function errorText(err: unknown, fallback: string): string {
  return err instanceof Error ? err.message : fallback;
}

function NotConfiguredCard({ reason }: { reason: string }) {
  return (
    <div className="cm-card cm-card-pad cm-rabbit-setup">
      <h2 className="cm-card-title">Dead-letter queues need the RabbitMQ management API</h2>
      <p className="cm-card-description cm-rabbit-hint">
        {reason.charAt(0).toUpperCase() + reason.slice(1)}. Set <code>rabbitmq_management_url</code>,{" "}
        <code>rabbitmq_management_username</code> and <code>rabbitmq_management_password</code> on{" "}
        <code>CeleryManagerConfig</code>. Browsing needs read permission on those queues, because RabbitMQ
        hands each message out and takes it back.{" "}
        <a className="cm-link" href={DOCS_URL} target="_blank" rel="noreferrer">
          How to set it up
        </a>
      </p>
    </div>
  );
}

function DeathSummary({ message }: { message: DeadLetterMessage }) {
  if (message.x_death.length === 0) return <span className="cm-dlq-masked">No x-death header</span>;
  return (
    <ul className="cm-dlq-death">
      {message.x_death.map((d, i) => (
        <li key={i}>
          <span className="cm-dlq-reason">{d.reason ?? "unknown"}</span> from <code>{d.queue ?? "?"}</code>
          {d.routing_keys.length > 0 && (
            <>
              {" "}
              (key <code>{d.routing_keys.join(", ")}</code>)
            </>
          )}
          {" · "}
          {d.count}×{d.time ? ` · last ${new Date(d.time).toLocaleString()}` : ""}
        </li>
      ))}
    </ul>
  );
}

function RequeueDialog({
  queue,
  keys,
  onClose,
  onDone,
}: {
  queue: string;
  // null means "all, up to ALL_LIMIT".
  keys: string[] | null;
  onClose: () => void;
  onDone: (result: RequeueResult) => void;
}) {
  const [force, setForce] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const what = keys ? `${keys.length} selected message${keys.length === 1 ? "" : "s"}` : `up to ${ALL_LIMIT} messages`;

  async function submit() {
    setBusy(true);
    setError(null);
    try {
      const result = await requeueDeadLetters(queue, keys ? { keys, force } : { limit: ALL_LIMIT, force });
      onDone(result);
    } catch (err) {
      setError(errorText(err, "Requeue failed."));
      setBusy(false);
    }
  }

  return (
    <Modal
      title={`Requeue ${what}?`}
      description={`Each message goes back to the exchange and routing key it was dead-lettered from, and is removed from ${queue} only once RabbitMQ has confirmed the copy. Recorded in the audit log.`}
      busy={busy}
      onClose={onClose}
    >
      <div className="cm-modal-body">
        <label className="cm-dlq-toggle">
          <span className="cm-checkbox">
            <input type="checkbox" checked={force} disabled={busy} onChange={(e) => setForce(e.target.checked)} />
            <IconCheck />
          </span>
          Also send messages that have died more than 3 times (they may loop)
        </label>
        {error && (
          <p className="cm-error" role="alert">
            {error}
          </p>
        )}
        <div className="cm-modal-actions">
          <button type="button" className="cm-btn cm-btn-outline" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="button" className="cm-btn cm-btn-primary" onClick={submit} disabled={busy} aria-busy={busy}>
            {busy ? "Requeueing…" : "Requeue"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

function ResultNotice({ result }: { result: RequeueResult }) {
  const moved = `Requeued ${result.moved} message${result.moved === 1 ? "" : "s"} from ${result.queue}.`;
  return (
    <div className={result.error ? "cm-error cm-dlq-result" : "cm-notice cm-dlq-result"} role="status">
      <p>
        {moved}
        {result.error ? ` Stopped: ${result.error}. The rest are still in the dead-letter queue.` : ""}
        {result.stopped ? ` Stopped early: ${result.stopped}.` : ""}
      </p>
      {result.refused.length > 0 && (
        <ul>
          {result.refused.map((r, i) => (
            <li key={i}>
              <code>{r.key ?? "message"}</code>: {r.reason}
            </li>
          ))}
        </ul>
      )}
      {result.not_found.length > 0 && (
        <p>Not found any more (taken by someone else?): {result.not_found.join(", ")}</p>
      )}
    </div>
  );
}

export function RabbitDeadLetters() {
  const canRequeue = useCan("admin");
  const [listing, setListing] = useState<DeadLetterQueueListing | NotConfigured | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [current, setCurrent] = useState<string | null>(null);
  const [reveal, setReveal] = useState(false);
  const [messages, setMessages] = useState<DeadLetterMessages | null>(null);
  const [loadingMessages, setLoadingMessages] = useState(false);
  const [messageError, setMessageError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [dialog, setDialog] = useState<{ keys: string[] | null } | null>(null);
  const [result, setResult] = useState<RequeueResult | null>(null);
  const requestIdRef = useRef(0);

  const loadList = useCallback(async () => {
    try {
      const next = await listDeadLetterQueues();
      setListing(next);
      setListError(null);
      if (next.configured) {
        setCurrent((cur) => (cur && next.queues.some((q) => q.name === cur) ? cur : next.queues[0]?.name ?? null));
      }
    } catch (err) {
      setListError(errorText(err, "Couldn't load the dead-letter queues."));
    }
  }, []);

  const loadMessages = useCallback(async (name: string, revealArgs: boolean) => {
    const requestId = ++requestIdRef.current;
    setLoadingMessages(true);
    setMessageError(null);
    try {
      const next = await browseDeadLetterQueue(name, revealArgs);
      if (requestId !== requestIdRef.current) return;
      setMessages(next.configured ? next : null);
    } catch (err) {
      if (requestId !== requestIdRef.current) return;
      setMessages(null);
      setMessageError(errorText(err, "Couldn't read the dead-letter queue."));
    } finally {
      if (requestId === requestIdRef.current) setLoadingMessages(false);
    }
  }, []);

  useEffect(() => {
    loadList();
  }, [loadList]);

  // Not polled: every look hands the messages out and back, so it happens
  // only when someone asks (opening a queue, Refresh, revealing arguments).
  useEffect(() => {
    setSelected(new Set());
    if (current) loadMessages(current, reveal);
    else setMessages(null);
  }, [current, reveal, loadMessages]);

  function toggle(key: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  const configured = listing?.configured ? listing : null;
  const queues = configured?.queues ?? [];
  const currentQueue = queues.find((q) => q.name === current) ?? null;
  const items = messages?.queue === current ? messages.messages : [];

  return (
    <div className="cm-page">
      <Link to="/queues" className="cm-btn cm-btn-ghost cm-btn-sm cm-back-link">
        <IconChevronLeft />
        Queues
      </Link>

      <header className="cm-page-header">
        <div className="cm-page-header-titles">
          <h1 className="cm-page-title">RabbitMQ dead-letter queues</h1>
          <p className="cm-page-lede">
            Messages RabbitMQ dead-lettered (rejected, expired or over a length limit), and a way to send them back to
            the queue they came from.
          </p>
        </div>
      </header>

      {result && <ResultNotice result={result} />}

      {listError && !listing ? (
        <div className="cm-card cm-card-pad cm-stack">
          <p className="cm-error" role="alert">
            {listError}
          </p>
          <button type="button" className="cm-btn cm-btn-outline" onClick={loadList}>
            Try again
          </button>
        </div>
      ) : !listing ? (
        <div className="cm-card cm-card-pad">
          <p className="cm-loading">
            <span className="cm-live-dot" aria-hidden="true" />
            Looking for dead-letter queues&hellip;
          </p>
        </div>
      ) : !listing.configured ? (
        <NotConfiguredCard reason={listing.reason} />
      ) : (
        <>
          {configured && configured.warnings.length > 0 && (
            <div className="cm-alert" role="status">
              <span>
                {configured.warnings.map((w) => (
                  <span key={w} className="cm-dlq-warn" style={{ display: "block" }}>
                    {w}.
                  </span>
                ))}
              </span>
            </div>
          )}
          {queues.length === 0 ? (
            <div className="cm-card cm-card-flush">
              <p className="cm-empty-state">
                No dead-letter queues found. A queue counts when another queue&rsquo;s{" "}
                <code>x-dead-letter-exchange</code> routes to it, or its name matches{" "}
                {configured?.patterns.map((p) => `"${p}"`).join(", ") || "a configured pattern"}.
              </p>
            </div>
          ) : (
            <div className="cm-dlq-layout">
              <nav className="cm-card cm-card-flush" aria-label="Dead-letter queues">
                <ul className="cm-dlq-list">
                  {queues.map((q) => (
                    <li key={q.name}>
                      <button
                        type="button"
                        className="cm-dlq-item"
                        aria-current={q.name === current ? "true" : undefined}
                        onClick={() => {
                          setResult(null);
                          setCurrent(q.name);
                        }}
                      >
                        <span className="cm-queue-name">{q.name}</span>
                        <span className="cm-dlq-count">
                          {q.messages_ready === null ? "—" : q.messages_ready.toLocaleString()} waiting ·{" "}
                          {q.matched_by.map((m) => MATCH_LABEL[m] ?? m).join(", ")}
                        </span>
                        {q.dead_letter_sources.length > 0 && (
                          <span className="cm-queue-sources">from {q.dead_letter_sources.join(", ")}</span>
                        )}
                      </button>
                    </li>
                  ))}
                </ul>
              </nav>

              <section className="cm-card cm-card-flush" aria-label={currentQueue ? `Messages in ${currentQueue.name}` : "Messages"}>
                <div className="cm-card-pad cm-stack">
                  <div className="cm-dlq-toolbar">
                    <div>
                      <h2 className="cm-card-title">{currentQueue?.name}</h2>
                      <p className="cm-card-description">
                        The first {messages?.limit ?? 20} messages, approximately: reading puts them back marked
                        redelivered and may change their order.
                      </p>
                    </div>
                    <div className="cm-dlq-toolbar-actions">
                      <label className="cm-dlq-toggle">
                        <span className="cm-checkbox">
                          <input type="checkbox" checked={reveal} onChange={(e) => setReveal(e.target.checked)} />
                          <IconCheck />
                        </span>
                        Show arguments
                      </label>
                      <button
                        type="button"
                        className="cm-btn cm-btn-outline cm-btn-sm"
                        onClick={() => current && loadMessages(current, reveal)}
                        disabled={loadingMessages}
                      >
                        Refresh
                      </button>
                      {canRequeue && (
                        <>
                          <button
                            type="button"
                            className="cm-btn cm-btn-outline cm-btn-sm"
                            disabled={selected.size === 0}
                            onClick={() => setDialog({ keys: [...selected] })}
                          >
                            Requeue selected{selected.size ? ` (${selected.size})` : ""}
                          </button>
                          <button
                            type="button"
                            className="cm-btn cm-btn-primary cm-btn-sm"
                            disabled={!currentQueue || currentQueue.messages_ready === 0}
                            onClick={() => setDialog({ keys: null })}
                          >
                            Requeue all…
                          </button>
                        </>
                      )}
                    </div>
                  </div>
                  {messageError && (
                    <p className="cm-error" role="alert">
                      {messageError}
                    </p>
                  )}
                </div>

                {loadingMessages && items.length === 0 ? (
                  <div className="cm-card-pad">
                    <p className="cm-loading">
                      <span className="cm-live-dot" aria-hidden="true" />
                      Reading messages&hellip;
                    </p>
                  </div>
                ) : items.length === 0 ? (
                  !messageError && <p className="cm-empty-state">This dead-letter queue is empty.</p>
                ) : (
                  <div className="cm-table-scroll" aria-busy={loadingMessages}>
                    <table className="cm-table cm-table-stack">
                      <thead>
                        <tr>
                          {canRequeue && (
                            <th scope="col" className="cm-select-cell">
                              <span className="cm-visually-hidden">Select</span>
                            </th>
                          )}
                          <th scope="col">Task</th>
                          <th scope="col">Why it was dead-lettered</th>
                          <th scope="col">Arguments</th>
                        </tr>
                      </thead>
                      <tbody>
                        {items.map((m, i) => (
                          <tr key={m.key ?? `row-${i}`}>
                            {canRequeue && (
                              <td className="cm-select-cell">
                                {m.key && (
                                  <span className="cm-checkbox">
                                    <input
                                      type="checkbox"
                                      checked={selected.has(m.key)}
                                      onChange={() => m.key && toggle(m.key)}
                                      aria-label={`Select ${m.task_name ?? "message"} ${m.key}`}
                                    />
                                    <IconCheck />
                                  </span>
                                )}
                              </td>
                            )}
                            <td className="cm-cell-primary">
                              {m.task_id ? (
                                <Link to={`/tasks/${m.task_id}`} className="cm-task-link" title={m.task_id}>
                                  <span className="cm-task-name">{m.task_name ?? "unknown task"}</span>
                                  <span className="cm-task-id">{m.task_id.slice(0, 8)}</span>
                                </Link>
                              ) : (
                                <span className="cm-task-name">{m.task_name ?? "not a Celery task"}</span>
                              )}
                              {m.requeued_by_celery_manager > 0 && (
                                <span className="cm-queue-sources">
                                  requeued {m.requeued_by_celery_manager}× from here before
                                </span>
                              )}
                            </td>
                            <td>
                              <DeathSummary message={m} />
                            </td>
                            <td>
                              {m.args !== null || m.kwargs !== null ? (
                                <span className="cm-dlq-args">
                                  {m.args ?? "()"} {m.kwargs && m.kwargs !== "{}" ? m.kwargs : ""}
                                </span>
                              ) : m.args_masked ? (
                                <span className="cm-dlq-masked">Hidden. Tick “Show arguments”.</span>
                              ) : (
                                <span className="cm-dlq-masked">—</span>
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </section>
            </div>
          )}
        </>
      )}

      {dialog && current && (
        <RequeueDialog
          queue={current}
          keys={dialog.keys}
          onClose={() => setDialog(null)}
          onDone={(r) => {
            setDialog(null);
            setResult(r);
            loadList();
            loadMessages(current, reveal);
            setSelected(new Set());
          }}
        />
      )}
    </div>
  );
}
