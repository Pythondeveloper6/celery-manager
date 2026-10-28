import { useEffect, useRef, useState } from "react";
import { getBrokerHealth, type AmqpDetails, type BrokerHealth, type RedisDetails } from "../api/broker";
import "../styles/broker.css";

// Above this share of maxmemory the bar turns to the warning colour.
const MEMORY_WARN_RATIO = 0.8;

function formatBytes(bytes: number): string {
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 10 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
}

function formatUptime(seconds: number): string {
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days > 0) return `${days}d ${hours}h`;
  if (hours > 0) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

function isRedis(details: BrokerHealth["details"]): details is RedisDetails {
  return !!details && "maxmemory" in details;
}

function Stat({ label, value, warn }: { label: string; value: string; warn?: boolean }) {
  return (
    <div className="cm-broker-stat" data-warn={warn ? "true" : undefined}>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function MemoryBar({ details }: { details: RedisDetails }) {
  if (details.used_memory === null) return null;
  if (details.maxmemory === 0) {
    // No limit: a bar against nothing would be a made-up percentage.
    return (
      <p className="cm-broker-memory-text">
        Memory: <strong>{formatBytes(details.used_memory)}</strong> used, no limit set (maxmemory is 0).
      </p>
    );
  }
  const ratio = details.memory_ratio ?? details.used_memory / details.maxmemory;
  const percent = Math.min(100, Math.round(ratio * 100));
  return (
    <div className="cm-broker-memory">
      <div className="cm-broker-memory-head">
        <span>Memory</span>
        <span>
          {formatBytes(details.used_memory)} of {formatBytes(details.maxmemory)} ({percent}%)
        </span>
      </div>
      <div
        className="cm-broker-bar"
        role="progressbar"
        aria-label="Redis memory used"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={percent}
        data-warn={ratio >= MEMORY_WARN_RATIO ? "true" : undefined}
      >
        <div className="cm-broker-bar-fill" style={{ width: `${percent}%` }} />
      </div>
    </div>
  );
}

function RedisBody({ details }: { details: RedisDetails }) {
  const policyOk = details.maxmemory_policy === "noeviction";
  const rdbFailed = (details.persistence.rdb_last_save_status ?? "ok") !== "ok";
  const aofFailed = details.persistence.aof_enabled && (details.persistence.aof_last_write_status ?? "ok") !== "ok";
  return (
    <>
      <MemoryBar details={details} />
      {details.warnings.map((warning) => (
        <p key={warning} className="cm-broker-warning" role="status">
          {warning}
        </p>
      ))}
      <dl className="cm-broker-stats">
        {details.maxmemory_policy && <Stat label="Eviction policy" value={details.maxmemory_policy} warn={!policyOk} />}
        {details.connected_clients !== null && (
          <Stat
            label="Clients"
            value={`${details.connected_clients.toLocaleString()}${
              details.blocked_clients ? ` (${details.blocked_clients.toLocaleString()} blocked)` : ""
            }`}
          />
        )}
        {details.evicted_keys !== null && (
          <Stat label="Evicted keys" value={details.evicted_keys.toLocaleString()} warn={details.evicted_keys > 0} />
        )}
        {!!details.rejected_connections && (
          <Stat label="Refused connections" value={details.rejected_connections.toLocaleString()} warn />
        )}
        {details.version && <Stat label="Redis" value={details.version} />}
        {details.uptime_seconds !== null && <Stat label="Uptime" value={formatUptime(details.uptime_seconds)} />}
        <Stat
          label="Persistence"
          value={[
            `RDB ${details.persistence.rdb_last_save_status ?? "unknown"}`,
            details.persistence.aof_enabled ? `AOF ${details.persistence.aof_last_write_status ?? "on"}` : "AOF off",
          ].join(", ")}
          warn={rdbFailed || aofFailed}
        />
      </dl>
    </>
  );
}

function AmqpBody({ details }: { details: AmqpDetails }) {
  return (
    <>
      <dl className="cm-broker-stats">
        <Stat label="Server" value={[details.product, details.version].filter(Boolean).join(" ") || "unknown"} />
        {details.cluster_name && <Stat label="Cluster" value={details.cluster_name} />}
      </dl>
      {details.management === "configured" ? (
        <p className="cm-broker-note">Memory and disk alarms are in the RabbitMQ panel below.</p>
      ) : (
        <p className="cm-broker-note">
          Memory and disk alarms need the RabbitMQ management API, which isn&rsquo;t configured, so none are shown here.
        </p>
      )}
    </>
  );
}

/** Broker status for the top of the Queues page. `refreshToken` changes each
 *  time the page refreshes, so the panel reloads with it and shares its
 *  paused state (a hidden tab asks nothing of the broker). */
export function BrokerPanel({ refreshToken }: { refreshToken: number }) {
  const [health, setHealth] = useState<BrokerHealth | null>(null);
  const [error, setError] = useState<string | null>(null);
  const requestIdRef = useRef(0);

  useEffect(() => {
    const requestId = ++requestIdRef.current;
    getBrokerHealth()
      .then((next) => {
        if (requestId !== requestIdRef.current) return;
        setHealth(next);
        setError(null);
      })
      .catch((err) => {
        if (requestId !== requestIdRef.current) return;
        setError(err instanceof Error ? err.message : "Couldn't load broker health.");
      });
  }, [refreshToken]);

  const transportLabel = health?.transport
    ? `${health.transport === "amqp" ? "RabbitMQ (AMQP)" : health.transport}${health.sentinel ? " Sentinel" : ""}`
    : null;

  return (
    <section className="cm-card cm-card-pad cm-broker" aria-label="Broker">
      <header className="cm-broker-header">
        <h2 className="cm-broker-title">Broker</h2>
        {health && (
          <span className="cm-broker-status" data-state={health.reachable ? "up" : "down"}>
            {health.reachable ? "Reachable" : "Unreachable"}
          </span>
        )}
        {health?.reachable && health.latency_ms !== null && (
          <span className="cm-broker-meta">
            {health.latency_ms.toLocaleString(undefined, { maximumFractionDigits: 1 })} ms
          </span>
        )}
        {transportLabel && <span className="cm-broker-meta">{transportLabel}</span>}
        {health?.url && <code className="cm-broker-url">{health.url}</code>}
      </header>

      {!health && !error && (
        <p className="cm-loading">
          <span className="cm-live-dot" aria-hidden="true" />
          Checking the broker&hellip;
        </p>
      )}
      {error && (
        <p className="cm-error" role="alert">
          {error}
          {health ? " Showing the last answer." : ""}
        </p>
      )}
      {health && !health.reachable && (
        <p className="cm-error" role="alert">
          {health.error ?? "The broker did not answer."}
        </p>
      )}
      {health?.reachable && isRedis(health.details) && <RedisBody details={health.details} />}
      {health?.reachable && health.details && !isRedis(health.details) && <AmqpBody details={health.details} />}
      {health?.reachable && !health.details && (
        <p className="cm-broker-note">
          Broker details aren&rsquo;t available for this transport
          {health.transport ? ` (${health.transport})` : ""}. Reachability and latency are all celery-manager can check.
        </p>
      )}
    </section>
  );
}
