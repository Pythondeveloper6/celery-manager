import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  getRabbitOverview,
  type NotConfigured,
  type RabbitListingMeta,
  type RabbitNode,
  type RabbitOverview,
  type RabbitQueueInfo,
} from "../api/rabbitmq";
import "../styles/rabbitmq.css";

/* ==========================================================================
   RabbitMQ on the Queues page: the node panel (memory, disk, descriptors,
   alarms) and the per-queue management details. Shown only when the broker
   is RabbitMQ; with no management URL configured it explains the setup
   instead. Everything here is read-only.
   ========================================================================== */

// Node resources change slowly and the management API samples every 5s;
// no point asking more often than the depth table does.
const REFRESH_MS = 10000;

const DOCS_URL = "https://pythondeveloper6.github.io/celery-manager/rabbitmq/";

export function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let n = value;
  let unit = 0;
  while (n >= 1024 && unit < units.length - 1) {
    n /= 1024;
    unit += 1;
  }
  return `${n >= 10 || unit === 0 ? Math.round(n) : n.toFixed(1)} ${units[unit]}`;
}

export function formatRate(rate: number | null | undefined): string {
  if (rate === null || rate === undefined) return "—";
  return `${rate >= 10 ? Math.round(rate).toLocaleString() : rate.toFixed(1)}/s`;
}

function Meter({ used, limit, alarm, label }: { used: number | null; limit: number | null; alarm: boolean; label: string }) {
  const share = used !== null && limit ? Math.min(1, used / limit) : null;
  return (
    <span className="cm-rabbit-meter" data-alarm={alarm ? "true" : undefined}>
      <span className="cm-rabbit-meter-track" aria-hidden="true">
        <span className="cm-rabbit-meter-fill" style={{ width: `${Math.round((share ?? 0) * 100)}%` }} />
      </span>
      <span className="cm-rabbit-meter-text">{label}</span>
    </span>
  );
}

function NodeRow({ node }: { node: RabbitNode }) {
  const memAlarm = !!node.mem_alarm;
  const diskAlarm = !!node.disk_free_alarm;
  return (
    <tr>
      <td className="cm-cell-primary">
        <span className="cm-queue-name">{node.name}</span>
        {node.running === false && <span className="cm-rabbit-flag">not running</span>}
      </td>
      <td>
        <span className="cm-queue-cell-label">Memory</span>
        <Meter
          used={node.mem_used}
          limit={node.mem_limit}
          alarm={memAlarm}
          label={`${formatBytes(node.mem_used)} of ${formatBytes(node.mem_limit)}${memAlarm ? " · alarm" : ""}`}
        />
      </td>
      <td>
        <span className="cm-queue-cell-label">Disk free</span>
        <span className="cm-rabbit-disk" data-alarm={diskAlarm ? "true" : undefined}>
          {formatBytes(node.disk_free)} free, limit {formatBytes(node.disk_free_limit)}
          {diskAlarm ? " · alarm" : ""}
        </span>
      </td>
      <td className="cm-col-fit cm-num">
        <span className="cm-queue-cell-label">File descriptors</span>
        {node.fd_used ?? "—"} / {node.fd_total ?? "—"}
      </td>
      <td className="cm-col-fit cm-num">
        <span className="cm-queue-cell-label">Sockets</span>
        {node.sockets_used ?? "—"} / {node.sockets_total ?? "—"}
      </td>
    </tr>
  );
}

function SetupHint({ reason }: { reason: string | null }) {
  return (
    <div className="cm-card cm-card-pad cm-rabbit-setup">
      <h2 className="cm-card-title">RabbitMQ details are off</h2>
      <p className="cm-card-description">
        {reason ? `${reason.charAt(0).toUpperCase()}${reason.slice(1)}. ` : ""}
        Set <code>rabbitmq_management_url</code>, <code>rabbitmq_management_username</code> and{" "}
        <code>rabbitmq_management_password</code> on <code>CeleryManagerConfig</code> to see consumers, unacknowledged
        messages, rates, node alarms and dead-letter queues here. A user with the <code>monitoring</code> tag is
        enough to read.{" "}
        <a className="cm-link" href={DOCS_URL} target="_blank" rel="noreferrer">
          How to set it up
        </a>
      </p>
    </div>
  );
}

/** The RabbitMQ panel above the queue table. `meta` is the `rabbitmq` part
 *  of the queue listing; nothing renders for other brokers. */
export function RabbitPanel({ meta, visible }: { meta: RabbitListingMeta | undefined; visible: boolean }) {
  const configured = !!meta?.configured;
  const [overview, setOverview] = useState<RabbitOverview | NotConfigured | null>(null);
  const [error, setError] = useState<string | null>(null);
  const requestIdRef = useRef(0);

  const refresh = useCallback(async () => {
    const requestId = ++requestIdRef.current;
    try {
      const next = await getRabbitOverview();
      if (requestId !== requestIdRef.current) return;
      setOverview(next);
      setError(null);
    } catch (err) {
      if (requestId !== requestIdRef.current) return;
      setError(err instanceof Error ? err.message : "Couldn't load RabbitMQ details.");
    }
  }, []);

  useEffect(() => {
    if (!configured || !visible) return;
    refresh();
    const interval = setInterval(refresh, REFRESH_MS);
    return () => clearInterval(interval);
  }, [configured, visible, refresh]);

  if (!meta || meta.is_rabbitmq === false) return null;
  if (!meta.configured) return meta.is_rabbitmq ? <SetupHint reason={meta.reason} /> : null;

  const data = overview && overview.configured ? overview : null;
  const alarmed = data ? data.nodes.filter((n) => n.mem_alarm || n.disk_free_alarm) : [];

  return (
    <section className="cm-card cm-card-flush cm-rabbit-panel" aria-labelledby="cm-rabbit-title">
      <div className="cm-card-pad cm-rabbit-head">
        <div>
          <h2 className="cm-card-title" id="cm-rabbit-title">
            RabbitMQ
            {data?.rabbitmq_version && <span className="cm-rabbit-version">{data.rabbitmq_version}</span>}
          </h2>
          <p className="cm-card-description">
            {data
              ? `Vhost ${data.vhost} · ${data.nodes.length} node${data.nodes.length === 1 ? "" : "s"} · publishing ${formatRate(
                  data.publish_rate,
                )}, delivering ${formatRate(data.deliver_rate)}`
              : "From the management API."}
          </p>
        </div>
        <Link to="/queues/dead-letter-queues" className="cm-btn cm-btn-outline cm-btn-sm">
          Dead-letter queues
        </Link>
      </div>

      {alarmed.length > 0 && (
        <div className="cm-card-pad cm-rabbit-alarm" role="alert">
          {alarmed.map((n) => (
            <p key={n.name}>
              <strong>{n.name}</strong> has a {[n.mem_alarm && "memory", n.disk_free_alarm && "disk"].filter(Boolean).join(" and ")}{" "}
              alarm. RabbitMQ blocks every publisher until it clears, so new tasks can&rsquo;t be sent.
            </p>
          ))}
        </div>
      )}

      {(error || meta.error) && (
        <div className="cm-card-pad">
          <p className="cm-error" role="alert">
            {error ?? meta.error}
          </p>
        </div>
      )}

      {!data && !error ? (
        <div className="cm-card-pad">
          <p className="cm-loading">
            <span className="cm-live-dot" aria-hidden="true" />
            Loading RabbitMQ details&hellip;
          </p>
        </div>
      ) : data && data.nodes.length > 0 ? (
        <div className="cm-table-scroll">
          <table className="cm-table cm-table-stack cm-queues-table cm-rabbit-nodes">
            <thead>
              <tr>
                <th scope="col">Node</th>
                <th scope="col">Memory</th>
                <th scope="col">Disk</th>
                <th scope="col" className="cm-col-fit cm-num" title="File descriptors used / available">
                  Descriptors
                </th>
                <th scope="col" className="cm-col-fit cm-num">
                  Sockets
                </th>
              </tr>
            </thead>
            <tbody>
              {data.nodes.map((node) => (
                <NodeRow key={node.name} node={node} />
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}

/** One line of management details under a queue's name. */
export function RabbitQueueDetails({ info }: { info: RabbitQueueInfo | null | undefined }) {
  if (!info) return null;
  const parts: string[] = [info.type];
  if (info.messages_unacknowledged !== null) parts.push(`${info.messages_unacknowledged.toLocaleString()} unacked`);
  if (info.publish_rate !== null || info.deliver_rate !== null) {
    parts.push(`in ${formatRate(info.publish_rate)} · out ${formatRate(info.deliver_rate)}`);
  }
  if (info.max_priority !== null) parts.push(`priority 0–${info.max_priority}`);
  if (info.dead_letter_exchange !== null) {
    const target = info.dead_letter_exchange === "" ? "default exchange" : info.dead_letter_exchange;
    parts.push(`dead-letters to ${target}${info.dead_letter_routing_key ? ` (${info.dead_letter_routing_key})` : ""}`);
  }
  if (info.idle_since) parts.push(`idle since ${new Date(info.idle_since.replace(" ", "T")).toLocaleString()}`);
  return <span className="cm-queue-sources cm-rabbit-queue">{parts.join(" · ")}</span>;
}
