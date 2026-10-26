import { useCallback, useEffect, useRef, useState } from "react";
import { listQueues, purgeQueue, type QueueInfo, type QueueListing } from "../api/queues";
import "../styles/queues.css";

const REFRESH_MS = 5000;

// Samples kept per queue for the sparkline: five minutes at one per refresh.
// Session-only and client-side by design — closing the tab forgets it, and
// nothing is stored on the server.
const HISTORY_SAMPLES = 60;

const SOURCE_LABELS: Record<string, string> = {
  default: "default",
  config: "configured",
  routes: "routed",
  runs: "seen in runs",
  workers: "consumed by workers",
};

/** The server's reason as a sentence: trailing period exactly once, and a
 *  capital first letter when it starts the sentence. */
function asSentence(text: string, capitalise: boolean): string {
  const trimmed = text.trim().replace(/\.+$/, "");
  const body = capitalise ? trimmed.charAt(0).toUpperCase() + trimmed.slice(1) : trimmed;
  return `${body}.`;
}

function Sparkline({ values, label }: { values: number[]; label: string }) {
  if (values.length < 2) return <span className="cm-queue-spark cm-queue-spark-empty" aria-hidden="true" />;
  const width = 72;
  const height = 20;
  // Scaled to this session's own low..high, not 0..high: the question a
  // sparkline answers is "is it growing or draining", and a queue moving
  // between 120 and 130 would be a flat line against zero. The label carries
  // the actual range.
  const low = Math.min(...values);
  const high = Math.max(...values);
  const span = Math.max(1, high - low);
  // Spread whatever has been collected across the full width, so a fresh page
  // shows a readable line after two refreshes rather than a dot in a corner.
  // The newest sample is always the right edge, marked with a dot.
  const step = (width - 3) / (values.length - 1);
  const coords = values.map((v, i) => [1.5 + i * step, height - 2 - ((v - low) / span) * (height - 4)]);
  const points = coords.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const [lastX, lastY] = coords[coords.length - 1];
  return (
    <svg
      className="cm-queue-spark"
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={`${label}: between ${low.toLocaleString()} and ${high.toLocaleString()}`}
    >
      <title>{`Between ${low.toLocaleString()} and ${high.toLocaleString()} this session`}</title>
      <polyline points={points} fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
      <circle cx={lastX} cy={lastY} r="1.75" fill="currentColor" />
    </svg>
  );
}

function PurgeConfirm({
  queue,
  onCancel,
  onPurged,
}: {
  queue: QueueInfo;
  onCancel: () => void;
  onPurged: (message: string) => void;
}) {
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const inputId = `cm-purge-${queue.name}`;
  const matches = typed === queue.name;

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  async function purge() {
    if (!matches) return;
    setBusy(true);
    setError(null);
    try {
      const result = await purgeQueue(queue.name);
      const removed = `${result.messages_removed.toLocaleString()} message${result.messages_removed === 1 ? "" : "s"}`;
      const marked =
        result.runs_marked_revoked > 0
          ? ` ${result.runs_marked_revoked.toLocaleString()} recorded run${result.runs_marked_revoked === 1 ? " was" : "s were"} marked revoked.`
          : "";
      onPurged(`Purged ${removed} from ${queue.name}.${marked}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Purge failed.");
      setBusy(false);
    }
  }

  return (
    <form
      className="cm-queue-purge"
      onSubmit={(e) => {
        e.preventDefault();
        purge();
      }}
      onKeyDown={(e) => {
        if (e.key === "Escape") onCancel();
      }}
    >
      <p className="cm-queue-purge-warn">
        Deletes every message waiting in <strong>{queue.name}</strong>
        {queue.depth !== null ? ` (${queue.depth.toLocaleString()} right now)` : ""}. Those tasks will never run, and
        this can&rsquo;t be undone. Tasks a worker has already reserved, including ones with an ETA, are not affected.
      </p>
      <div className="cm-queue-purge-row">
        <label className="cm-queue-purge-label" htmlFor={inputId}>
          Type <code>{queue.name}</code> to confirm
        </label>
        <input
          ref={inputRef}
          id={inputId}
          className="cm-input cm-input-code cm-queue-purge-input"
          value={typed}
          autoComplete="off"
          spellCheck={false}
          onChange={(e) => setTyped(e.target.value)}
        />
        <button type="button" className="cm-btn cm-btn-outline cm-btn-sm" onClick={onCancel} disabled={busy}>
          Keep messages
        </button>
        <button
          type="submit"
          className="cm-btn cm-btn-destructive cm-btn-sm"
          disabled={!matches || busy}
          aria-busy={busy}
        >
          {busy ? "Purging…" : "Purge queue"}
        </button>
      </div>
      {error && (
        <p className="cm-error" role="alert">
          {error}
        </p>
      )}
    </form>
  );
}

export function Queues() {
  const [listing, setListing] = useState<QueueListing | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [visible, setVisible] = useState(() => document.visibilityState === "visible");
  const [purging, setPurging] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const historyRef = useRef(new Map<string, number[]>());
  const requestIdRef = useRef(0);

  const refresh = useCallback(async () => {
    const requestId = ++requestIdRef.current;
    try {
      const next = await listQueues();
      if (requestId !== requestIdRef.current) return;
      for (const queue of next.queues) {
        if (queue.depth === null) continue;
        const samples = historyRef.current.get(queue.name) ?? [];
        samples.push(queue.depth);
        if (samples.length > HISTORY_SAMPLES) samples.shift();
        historyRef.current.set(queue.name, samples);
      }
      setListing(next);
      setError(null);
    } catch (err) {
      if (requestId !== requestIdRef.current) return;
      setError(err instanceof Error ? err.message : "Couldn't load queues.");
    }
  }, []);

  useEffect(() => {
    const onVisibility = () => setVisible(document.visibilityState === "visible");
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, []);

  // A hidden tab asks nothing of the broker. Coming back refreshes at once,
  // then resumes the interval.
  useEffect(() => {
    if (!visible) return;
    refresh();
    const interval = setInterval(refresh, REFRESH_MS);
    return () => clearInterval(interval);
  }, [visible, refresh]);

  const broker = listing?.broker;
  const brokerUsable = !!broker && !broker.error;

  return (
    <div className="cm-page">
      <header className="cm-page-header">
        <div className="cm-page-header-titles">
          <h1 className="cm-page-title">Queues</h1>
          <p className="cm-page-lede">Messages waiting on the broker, per queue, refreshed every few seconds.</p>
        </div>
        {visible ? (
          <div className="cm-live-indicator" title={`Refreshes every ${REFRESH_MS / 1000} seconds`}>
            <span className="cm-live-dot" aria-hidden="true" />
            Live
          </div>
        ) : (
          <div className="cm-live-indicator" title="Paused while this tab is hidden">
            <span className="cm-paused-dot" aria-hidden="true" />
            Paused
          </div>
        )}
      </header>

      {broker?.error && (
        <p className="cm-alert" role="status">
          <span>
            {broker.unsupported ? "Broker depth isn't available: " : ""}
            {asSentence(broker.error, !broker.unsupported)}{" "}
            {broker.unsupported
              ? "Recorded counts below still come from celery-manager's own history."
              : "Recorded counts below still work; depth will return when the broker answers."}
          </span>
        </p>
      )}
      {notice && (
        <p className="cm-notice" role="status">
          {notice}
        </p>
      )}

      <div className="cm-card cm-card-flush">
        {error && !listing ? (
          <div className="cm-card-pad cm-stack">
            <p className="cm-error" role="alert">
              {error}
            </p>
            <button type="button" className="cm-btn cm-btn-outline" onClick={refresh}>
              Try again
            </button>
          </div>
        ) : !listing ? (
          <div className="cm-card-pad">
            <p className="cm-loading">
              <span className="cm-live-dot" aria-hidden="true" />
              Loading queues&hellip;
            </p>
          </div>
        ) : listing.queues.length === 0 ? (
          <p className="cm-empty-state">
            No queues known yet. Queues appear once they are configured, routed to, or seen on a recorded run.
          </p>
        ) : (
          <>
            {error && (
              <div className="cm-card-pad">
                <p className="cm-error" role="alert">
                  {error} Showing the last numbers loaded; retrying every few seconds.
                </p>
              </div>
            )}
            <div className="cm-table-scroll">
              <table className="cm-table cm-table-stack cm-queues-table">
                <thead>
                  <tr>
                    <th scope="col">Queue</th>
                    <th scope="col" className="cm-col-fit cm-num">
                      Broker depth
                    </th>
                    <th scope="col" className="cm-col-fit">
                      <span className="cm-visually-hidden">Depth this session</span>
                    </th>
                    <th scope="col" className="cm-col-fit cm-num">
                      Consumers
                    </th>
                    <th scope="col" className="cm-col-fit cm-num" title="Runs celery-manager has recorded as queued">
                      Recorded queued
                    </th>
                    <th scope="col" className="cm-col-fit">
                      <span className="cm-visually-hidden">Actions</span>
                    </th>
                  </tr>
                </thead>
                {listing.queues.map((q) => (
                  <tbody key={q.name}>
                    <tr>
                      <td className="cm-cell-primary">
                        <span className="cm-queue-name">{q.name}</span>
                        <span className="cm-queue-sources">
                          {q.sources.map((s) => SOURCE_LABELS[s] ?? s).join(" · ")}
                        </span>
                      </td>
                      <td className="cm-col-fit cm-num">
                        <span className="cm-queue-cell-label">Broker depth</span>
                        {q.depth === null ? (
                          <span className="cm-queue-unknown" title={q.reason ?? undefined}>
                            —
                          </span>
                        ) : (
                          <span className="cm-queue-depth" data-busy={q.depth > 0 ? "true" : undefined} title={q.reason ?? undefined}>
                            {q.depth.toLocaleString()}
                          </span>
                        )}
                      </td>
                      <td className="cm-col-fit cm-queue-spark-cell">
                        <Sparkline
                          values={historyRef.current.get(q.name) ?? []}
                          label={`Depth of ${q.name} over this session`}
                        />
                      </td>
                      <td className="cm-col-fit cm-num">
                        <span className="cm-queue-cell-label">Consumers</span>
                        {q.consumers === null ? (
                          <span
                            className="cm-queue-unknown"
                            title={
                              broker?.transport && brokerUsable
                                ? `The ${broker.transport} transport doesn't report consumers`
                                : undefined
                            }
                          >
                            —
                          </span>
                        ) : (
                          q.consumers.toLocaleString()
                        )}
                      </td>
                      <td className="cm-col-fit cm-num">
                        <span className="cm-queue-cell-label">Recorded queued</span>
                        {q.recorded_queued.toLocaleString()}
                      </td>
                      <td className="cm-col-fit cm-queue-actions">
                        {purging !== q.name && (
                          <button
                            type="button"
                            className="cm-btn cm-btn-destructive-outline cm-btn-sm"
                            onClick={() => {
                              setNotice(null);
                              setPurging(q.name);
                            }}
                            disabled={!brokerUsable}
                            title={brokerUsable ? undefined : "The broker can't be reached from here"}
                          >
                            Purge…
                          </button>
                        )}
                      </td>
                    </tr>
                    {purging === q.name && (
                      <tr className="cm-queue-purge-tr">
                        <td colSpan={6}>
                          <PurgeConfirm
                            queue={q}
                            onCancel={() => setPurging(null)}
                            onPurged={(message) => {
                              setPurging(null);
                              setNotice(message);
                              refresh();
                            }}
                          />
                        </td>
                      </tr>
                    )}
                  </tbody>
                ))}
              </table>
            </div>
          </>
        )}
      </div>

      {listing && (
        <ul className="cm-queues-notes">
          <li>
            <strong>Broker depth</strong> is what the broker is holding right now. <strong>Recorded queued</strong> is
            what celery-manager has seen sent but not yet started; it lags the broker and also counts tasks a worker has
            reserved for later, so the two often differ.
          </li>
          {listing.recorded_queued_unassigned > 0 && (
            <li>
              {listing.recorded_queued_unassigned.toLocaleString()} queued run
              {listing.recorded_queued_unassigned === 1 ? " has" : "s have"} no recorded queue yet and{" "}
              {listing.recorded_queued_unassigned === 1 ? "isn't" : "aren't"} counted above.
            </li>
          )}
          {broker?.workers_replied === false && <li>No workers answered, so queues only workers know about may be missing.</li>}
          <li>The sparkline is this browser session only; it starts empty each time you open the page.</li>
        </ul>
      )}
    </div>
  );
}
