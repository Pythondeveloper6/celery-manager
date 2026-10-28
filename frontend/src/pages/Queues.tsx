import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { BrokerPanel } from "../components/BrokerPanel";
import { RabbitPanel, RabbitQueueDetails } from "../components/RabbitPanel";
import {
  getQueueHistory,
  listQueues,
  purgeQueue,
  type DepthHistory,
  type QueueInfo,
  type QueueListing,
} from "../api/queues";
import "../styles/queues.css";
import { DepthChart } from "../components/TrendCharts";
import { useCan } from "../lib/role";
import { formatDuration } from "../lib/taskProgress";

const REFRESH_MS = 5000;

// Samples kept per queue for the sparkline: five minutes at one per refresh.
// Session-only and client-side by design — closing the tab forgets it, and
// nothing is stored on the server.
const HISTORY_SAMPLES = 60;

// The sparklines show the server's recorded depth over this many hours, and
// are refetched this often: samples arrive once a minute, so faster is waste.
const SPARK_HOURS = 24;
const HISTORY_REFRESH_MS = 60000;
const CHART_RANGES = [
  { hours: 1, label: "1 hour" },
  { hours: 6, label: "6 hours" },
  { hours: 24, label: "24 hours" },
  { hours: 168, label: "7 days" },
];

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

function Sparkline({ values, label, scope }: { values: number[]; label: string; scope: string }) {
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
      <title>{`Between ${low.toLocaleString()} and ${high.toLocaleString()} ${scope}`}</title>
      <polyline points={points} fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
      <circle cx={lastX} cy={lastY} r="1.75" fill="currentColor" />
    </svg>
  );
}

/** Larger chart of one queue's recorded depth, opened from its sparkline. */
function DepthPanel({ queue }: { queue: string }) {
  const [hours, setHours] = useState(24);
  const [data, setData] = useState<DepthHistory | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let current = true;
    setData(null);
    setError(null);
    getQueueHistory(queue, hours)
      .then((result) => current && setData(result))
      .catch((err) => current && setError(err instanceof Error ? err.message : "Couldn't load the depth history."));
    return () => {
      current = false;
    };
  }, [queue, hours]);

  return (
    <div className="cm-queue-depth-panel">
      <div className="cm-queue-depth-range" role="group" aria-label={`Time range for ${queue}`}>
        {CHART_RANGES.map((r) => (
          <button
            key={r.hours}
            type="button"
            className="cm-btn cm-btn-outline cm-btn-sm"
            aria-pressed={hours === r.hours}
            onClick={() => setHours(r.hours)}
          >
            {r.label}
          </button>
        ))}
      </div>
      {error ? (
        <p className="cm-error" role="alert">
          {error}
        </p>
      ) : !data ? (
        <p className="cm-loading">
          <span className="cm-live-dot" aria-hidden="true" />
          Loading depth history&hellip;
        </p>
      ) : data.points.length === 0 ? (
        <p className="cm-empty-state">
          No depth recorded for {queue} in this period. Depth is sampled by the events listener
          (<code>celery-manager events</code>) unless <code>queue_sample_interval_seconds</code> is 0.
        </p>
      ) : (
        <DepthChart points={data.points} hours={hours} queue={queue} />
      )}
    </div>
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
  // Purge deletes messages for good, so it is admin-only (server-enforced).
  const canPurge = useCan("admin");
  const [listing, setListing] = useState<QueueListing | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [visible, setVisible] = useState(() => document.visibilityState === "visible");
  const [purging, setPurging] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [charted, setCharted] = useState<string | null>(null);
  // Server-recorded depth per queue (see control/depth_history.py), used for
  // the sparklines once there are two points; until then the session samples.
  const [recorded, setRecorded] = useState<Record<string, number[]>>({});
  const historyRef = useRef(new Map<string, number[]>());
  const requestIdRef = useRef(0);
  // Bumped on every refresh so the broker panel reloads with the page.
  const [refreshToken, setRefreshToken] = useState(0);

  const refresh = useCallback(async () => {
    const requestId = ++requestIdRef.current;
    setRefreshToken((n) => n + 1);
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

  const queueNames = listing?.queues.map((queue) => queue.name).join("\n") ?? "";
  useEffect(() => {
    if (!visible || !queueNames) return;
    let current = true;
    const load = () => {
      for (const name of queueNames.split("\n")) {
        getQueueHistory(name, SPARK_HOURS)
          .then((result) => {
            if (current) setRecorded((prev) => ({ ...prev, [name]: result.points.map((p) => p.depth) }));
          })
          .catch(() => {
            // A sparkline that can't load stays on the session samples.
          });
      }
    };
    load();
    const interval = setInterval(load, HISTORY_REFRESH_MS);
    return () => {
      current = false;
      clearInterval(interval);
    };
  }, [visible, queueNames]);

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

      <BrokerPanel refreshToken={refreshToken} />
      <RabbitPanel meta={listing?.rabbitmq} visible={visible} />

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
                      <span className="cm-visually-hidden">Depth history; select to open the chart</span>
                    </th>
                    <th scope="col" className="cm-col-fit cm-num">
                      Consumers
                    </th>
                    <th scope="col" className="cm-col-fit cm-num" title="Runs celery-manager has recorded as queued">
                      Recorded queued
                    </th>
                    <th
                      scope="col"
                      className="cm-col-fit cm-num"
                      title={`How long runs waited in the queue before a worker started them, over the last ${Math.round((listing.wait_window_seconds ?? 900) / 60)} minutes. Time until an ETA or countdown is not counted.`}
                    >
                      Wait p50 / p95
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
                        <Link
                          to={`/queues/${encodeURIComponent(q.name)}`}
                          className="cm-queue-name cm-link"
                          title="See the messages waiting in this queue"
                        >
                          {q.name}
                        </Link>
                        <span className="cm-queue-sources">
                          {q.sources.map((s) => SOURCE_LABELS[s] ?? s).join(" · ")}
                        </span>
                        <RabbitQueueDetails info={q.rabbitmq} />
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
                        <button
                          type="button"
                          className="cm-queue-spark-btn"
                          aria-expanded={charted === q.name}
                          aria-label={`${charted === q.name ? "Hide" : "Show"} the depth chart for ${q.name}`}
                          onClick={() => setCharted(charted === q.name ? null : q.name)}
                        >
                          {(recorded[q.name]?.length ?? 0) >= 2 ? (
                            <Sparkline
                              values={recorded[q.name]}
                              label={`Depth of ${q.name} over the last ${SPARK_HOURS} hours`}
                              scope={`in the last ${SPARK_HOURS} hours`}
                            />
                          ) : (
                            <Sparkline
                              values={historyRef.current.get(q.name) ?? []}
                              label={`Depth of ${q.name} over this session`}
                              scope="this session"
                            />
                          )}
                        </button>
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
                      <td className="cm-col-fit cm-num">
                        <span className="cm-queue-cell-label">Wait p50 / p95</span>
                        {q.wait ? (
                          <span
                            title={`${q.wait.runs.toLocaleString()} run${q.wait.runs === 1 ? "" : "s"}; longest ${formatDuration(q.wait.max)}`}
                          >
                            {formatDuration(q.wait.p50)} / {formatDuration(q.wait.p95)}
                          </span>
                        ) : (
                          <span className="cm-queue-unknown" title="No run with a recorded send time started recently">
                            —
                          </span>
                        )}
                      </td>
                      <td className="cm-col-fit cm-queue-actions">
                        {canPurge && purging !== q.name && (
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
                    {charted === q.name && (
                      <tr className="cm-queue-purge-tr">
                        <td colSpan={7}>
                          <DepthPanel queue={q.name} />
                        </td>
                      </tr>
                    )}
                    {purging === q.name && (
                      <tr className="cm-queue-purge-tr">
                        <td colSpan={7}>
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
          <li>
            <strong>Wait</strong> is how long runs sat in the queue before a worker started them (median / 95th
            percentile, last {Math.round((listing.wait_window_seconds ?? 900) / 60)} minutes). It needs the events
            listener to have seen the task being sent, and leaves out time spent waiting for an ETA or countdown.
          </li>
          <li>
            The sparkline is the depth recorded by the events listener over the last {SPARK_HOURS} hours; select it for
            a larger chart. Until two samples exist it shows this browser session instead.
          </li>
          <li>Click a queue&rsquo;s name to see the messages waiting in it.</li>
        </ul>
      )}
    </div>
  );
}
