import "../styles/alerts.css";

import { useCallback, useEffect, useId, useState } from "react";
import type { FormEvent } from "react";
import {
  createChannel,
  createRule,
  deleteChannel,
  deleteRule,
  listChannels,
  listIncidents,
  listRules,
  testChannel,
  updateChannel,
  updateRule,
  type AlertChannel,
  type AlertIncident,
  type AlertRule,
  type ChannelInput,
  type ChannelKind,
  type RuleKind,
} from "../api/alerts";
import { Modal } from "../components/Modal";
import { IconCheck } from "../components/icons";
import { formatPercent, formatWhen } from "../lib/insights";
import { needsRole, useCan } from "../lib/role";

/* ==========================================================================
   Alerts: rules (what to watch), channels (where messages go) and recent
   incidents. Everyone signed in can read all three; only an admin changes
   them. The server masks channel URLs and never returns secrets, so the
   channel dialog edits "blind": an empty URL or secret field keeps what is
   stored.
   ========================================================================== */

const KIND_LABEL: Record<RuleKind, string> = {
  task_failed: "Task failed",
  consecutive_failures: "Consecutive failures",
  run_stuck: "Run stuck",
  worker_lost: "Worker lost",
  beat_stale: "Beat stale",
  task_sla: "Task sla",
  duration_regression: "Duration regression",
  daily_digest: "Daily digest",
  failure_rate: "Failure rate",
  queue_backlog: "Queue backlog",
  worker_offline: "Worker offline",
  missed_run: "Missed run",
  broker_unreachable: "Broker unreachable",
  broker_memory: "Broker memory high",
  queue_wait: "Queue wait time",
  queue_growing: "Queue growing",
  rabbitmq_alarm: "RabbitMQ alarm",
};

// About the one broker, not a named thing: no target field, the server pins it to "*".
const BROKER_KINDS: RuleKind[] = ["broker_unreachable", "broker_memory"];

const TARGET_LABEL: Record<RuleKind, string> = {
  task_failed: "Task name",
  consecutive_failures: "Task name",
  run_stuck: "Task name",
  worker_lost: "Task name",
  beat_stale: "Beat",
  task_sla: "Task name",
  duration_regression: "Task name",
  daily_digest: "Task name",
  failure_rate: "Task name",
  queue_backlog: "Queue name",
  worker_offline: "Worker name",
  missed_run: "Schedule name",
  broker_unreachable: "Broker",
  broker_memory: "Broker",
  queue_wait: "Queue name",
  queue_growing: "Queue name",
  rabbitmq_alarm: "RabbitMQ node",
};

const TARGET_PLACEHOLDER: Record<RuleKind, string> = {
  task_failed: "* or e.g. reports.build",
  consecutive_failures: "* or e.g. reports.build",
  run_stuck: "* or e.g. reports.build",
  worker_lost: "* or e.g. reports.build",
  beat_stale: "*",
  task_sla: "* or e.g. reports.build",
  duration_regression: "* or e.g. reports.build",
  daily_digest: "* or e.g. reports.build",
  failure_rate: "e.g. reports.build",
  queue_backlog: "e.g. celery",
  worker_offline: "e.g. celery@web-*",
  missed_run: "e.g. nightly-report",
  broker_unreachable: "*",
  broker_memory: "*",
  queue_wait: "e.g. emails",
  queue_growing: "e.g. celery",
  rabbitmq_alarm: "* or e.g. rabbit@node-1",
};

// Per kind, the threshold fields in the order shown. Mirrors RULE_PARAMS in
// control/alerts.py (the server is the authority and fills defaults).
// `percent` fields are entered as 1-100 and sent as a fraction.
const PARAM_FIELDS: Record<
  RuleKind,
  { key: string; label: string; hint: string; fallback: number; min: number; max: number; percent?: boolean }[]
> = {
  task_failed: [{"key": "window_minutes", "label": "Window minutes", "hint": "task failed: window minutes.", "fallback": 60, "min": 1, "max": 10080}],
  consecutive_failures: [{"key": "count", "label": "Count", "hint": "consecutive failures: count.", "fallback": 3, "min": 1, "max": 1000}, {"key": "window_minutes", "label": "Window minutes", "hint": "consecutive failures: window minutes.", "fallback": 1440, "min": 1, "max": 43200}],
  run_stuck: [{"key": "seconds", "label": "Seconds", "hint": "run stuck: seconds.", "fallback": 3600, "min": 1, "max": 604800}, {"key": "p95_multiplier", "label": "P95 multiplier", "hint": "run stuck: p95 multiplier.", "fallback": 3, "min": 1, "max": 100}, {"key": "min_runs", "label": "Min runs", "hint": "run stuck: min runs.", "fallback": 10, "min": 2, "max": 10000}],
  worker_lost: [{"key": "seconds", "label": "Seconds", "hint": "worker lost: seconds.", "fallback": 120, "min": 1, "max": 86400}],
  beat_stale: [{"key": "seconds", "label": "Seconds", "hint": "beat stale: seconds.", "fallback": 120, "min": 30, "max": 86400}],
  task_sla: [{"key": "seconds", "label": "Seconds", "hint": "task sla: seconds.", "fallback": 3600, "min": 1, "max": 604800}, {"key": "window_minutes", "label": "Window minutes", "hint": "task sla: window minutes.", "fallback": 60, "min": 1, "max": 10080}],
  duration_regression: [{"key": "multiplier", "label": "Multiplier", "hint": "duration regression: multiplier.", "fallback": 2, "min": 1.01, "max": 100}, {"key": "baseline_days", "label": "Baseline days", "hint": "duration regression: baseline days.", "fallback": 30, "min": 1, "max": 365}, {"key": "recent_minutes", "label": "Recent minutes", "hint": "duration regression: recent minutes.", "fallback": 60, "min": 1, "max": 43200}, {"key": "min_runs", "label": "Min runs", "hint": "duration regression: min runs.", "fallback": 10, "min": 2, "max": 10000}],
  daily_digest: [{"key": "hour_utc", "label": "Hour utc", "hint": "daily digest: hour utc.", "fallback": 8, "min": 0, "max": 23}],
  failure_rate: [
    { key: "rate", label: "Failure rate (%)", hint: "Fire when at least this share of finished runs failed.", fallback: 0.2, min: 1, max: 100, percent: true },
    { key: "min_runs", label: "Minimum runs", hint: "Fewer finished runs than this never fire: 1 of 1 is noise.", fallback: 10, min: 1, max: 1000000 },
    { key: "window_minutes", label: "Window (minutes)", hint: "Runs that finished within this many minutes count.", fallback: 15, min: 1, max: 10080 },
  ],
  queue_backlog: [
    { key: "depth", label: "Waiting messages", hint: "Fire when the broker holds at least this many messages in the queue.", fallback: 1000, min: 1, max: 1000000000 },
  ],
  worker_offline: [
    { key: "minutes", label: "Offline for (minutes)", hint: "Fire when a worker seen before hasn't answered a ping for this long.", fallback: 5, min: 1, max: 10080 },
  ],
  missed_run: [
    { key: "window_minutes", label: "Look back (minutes)", hint: "Fire while a run due within this many minutes never arrived.", fallback: 60, min: 1, max: 10080 },
  ],
  broker_unreachable: [
    { key: "minutes", label: "Unreachable for (minutes)", hint: "Fire when the broker hasn't accepted a connection for this long.", fallback: 2, min: 1, max: 10080 },
  ],
  broker_memory: [
    { key: "ratio", label: "Memory used (%)", hint: "Redis only. Fire when used memory reaches this share of maxmemory. Never fires when maxmemory is 0 (no limit).", fallback: 0.8, min: 1, max: 100, percent: true },
  ],
  queue_wait: [
    { key: "seconds", label: "Wait (seconds)", hint: "Fire when the chosen percentile of queue wait is at least this long. Time until an ETA or countdown doesn't count.", fallback: 60, min: 0.001, max: 86400 },
    { key: "quantile", label: "Percentile", hint: "95 judges the slowest 5% of runs (p95); 50 is the median.", fallback: 95, min: 1, max: 100 },
    { key: "window_minutes", label: "Window (minutes)", hint: "Runs that started within this many minutes count.", fallback: 15, min: 1, max: 10080 },
    { key: "min_runs", label: "Minimum runs", hint: "Fewer started runs than this never fire.", fallback: 5, min: 1, max: 1000000 },
  ],
  queue_growing: [
    { key: "minutes", label: "Growing for (minutes)", hint: "Look at depth samples from this long. Needs the events listener's depth sampler running.", fallback: 15, min: 1, max: 10080 },
    { key: "min_increase", label: "Increase (messages)", hint: "Fire when the queue gained at least this many messages without draining on the way.", fallback: 50, min: 1, max: 1000000000 },
  ],
  // RabbitMQ: nothing to tune; fires while a node reports a memory or disk alarm.
  rabbitmq_alarm: [],
};

const CHANNEL_LABEL: Record<ChannelKind, string> = { slack: "Slack", webhook: "Webhook", email: "Email", teams: "Microsoft Teams" };

function errorText(err: unknown, fallback: string): string {
  return err instanceof Error ? err.message : fallback;
}

function describeCondition(rule: AlertRule): string {
  const p = rule.params;
  switch (rule.kind) {
    case "failure_rate":
      return `${formatPercent(p.rate ?? 0)} of at least ${p.min_runs} runs fail within ${p.window_minutes} min`;
    case "queue_backlog":
      return `${p.depth} or more messages waiting`;
    case "worker_offline":
      return `No answer for ${p.minutes} min`;
    case "missed_run":
      return `A run due in the last ${p.window_minutes} min never arrived`;
    case "broker_unreachable":
      return `Broker unreachable for ${p.minutes} min`;
    case "broker_memory":
      return `Redis memory at ${formatPercent(p.ratio ?? 0)} of maxmemory or more`;
    case "queue_wait":
      return `p${p.quantile} queue wait of ${p.seconds}s or more over ${p.window_minutes} min (at least ${p.min_runs} runs)`;
    case "queue_growing":
      return `Depth up by ${p.min_increase} or more over ${p.minutes} min, without draining`;
    case "rabbitmq_alarm":
      return "A RabbitMQ node reports a memory or disk alarm";
    default:
      return Object.entries(p).map(([key, value]) => `${key.replaceAll("_", " ")}: ${value}`).join(" · ");
  }
}

function channelDestination(channel: AlertChannel): string {
  if (channel.kind === "email") return (channel.config.to ?? []).join(", ");
  const url = channel.config.url ?? "—";
  return channel.kind === "webhook" && channel.config.secret_set ? `${url} (signed)` : url;
}

function RuleState({ rule }: { rule: AlertRule }) {
  if (rule.state === "disabled") return <span className="cm-badge">Disabled</span>;
  if (rule.state === "firing") {
    return (
      <span className="cm-alert-state" data-state="firing" title={rule.firing_keys.join(", ")}>
        Firing{rule.firing_keys.length > 1 ? ` · ${rule.firing_keys.length}` : ""}
      </span>
    );
  }
  if (!rule.last_checked_at) return <span className="cm-badge">Not checked yet</span>;
  return (
    <span className="cm-alert-state" data-state="ok">
      OK
    </span>
  );
}

// --- dialogs ------------------------------------------------------------------

function RuleDialog({
  rule,
  channels,
  onClose,
  onSaved,
}: {
  rule: AlertRule | null;
  channels: AlertChannel[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const uid = useId();
  const [name, setName] = useState(rule?.name ?? "");
  const [kind, setKind] = useState<RuleKind>(rule?.kind ?? "failure_rate");
  const [target, setTarget] = useState(rule?.target ?? "*");
  // Kept as strings while editing so a cleared field isn't forced to 0.
  const [params, setParams] = useState<Record<string, string>>(() => paramDraft(rule?.kind ?? "failure_rate", rule?.params));
  const [quiet, setQuiet] = useState(String(rule?.quiet_period_minutes ?? 60));
  const [channelIds, setChannelIds] = useState<number[]>(rule?.channel_ids ?? []);
  const [notifyResolved, setNotifyResolved] = useState(rule?.notify_resolved ?? true);
  const [enabled, setEnabled] = useState(rule?.enabled ?? true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function paramDraft(k: RuleKind, values?: Record<string, number>): Record<string, string> {
    const draft: Record<string, string> = {};
    for (const field of PARAM_FIELDS[k]) {
      const value = values?.[field.key] ?? field.fallback;
      draft[field.key] = String(field.percent ? Math.round(value * 1000) / 10 : value);
    }
    return draft;
  }

  function changeKind(next: RuleKind) {
    setKind(next);
    setParams(paramDraft(next, next === rule?.kind ? rule.params : undefined));
  }

  function toggleChannel(id: number) {
    setChannelIds((ids) => (ids.includes(id) ? ids.filter((i) => i !== id) : [...ids, id]));
  }

  const brokerKind = BROKER_KINDS.includes(kind);
  const canSubmit = !busy && name.trim() !== "" && (brokerKind || target.trim() !== "");

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!canSubmit) return;
    const numeric: Record<string, number> = {};
    for (const field of PARAM_FIELDS[kind]) {
      const value = Number(params[field.key]);
      if (params[field.key].trim() === "" || Number.isNaN(value)) {
        setError(`${field.label} needs a number.`);
        return;
      }
      numeric[field.key] = field.percent ? value / 100 : value;
    }
    const input = {
      name: name.trim(),
      kind,
      target: brokerKind ? "*" : target.trim(),
      params: numeric,
      channel_ids: channelIds,
      quiet_period_minutes: Number(quiet),
      notify_resolved: notifyResolved,
      enabled,
    };
    setBusy(true);
    setError(null);
    try {
      if (rule) await updateRule(rule.id, input);
      else await createRule(input);
      onSaved();
    } catch (err) {
      setError(errorText(err, "Couldn't save the rule."));
      setBusy(false);
    }
  }

  return (
    <Modal
      title={rule ? `Edit ${rule.name}` : "Add alert rule"}
      description="Changes are recorded in the audit log and apply from the evaluator's next check."
      busy={busy}
      onClose={onClose}
    >
      <form className="cm-modal-body" onSubmit={submit} noValidate>
        <div className="cm-field">
          <label htmlFor={`${uid}-name`}>Name</label>
          <input id={`${uid}-name`} className="cm-input" value={name} maxLength={100} disabled={busy}
            onChange={(e) => setName(e.target.value)} autoComplete="off" />
        </div>
        <div className="cm-field">
          <label htmlFor={`${uid}-kind`}>Alert when</label>
          <select id={`${uid}-kind`} className="cm-select" value={kind} disabled={busy}
            onChange={(e) => changeKind(e.target.value as RuleKind)}>
            {(Object.keys(KIND_LABEL) as RuleKind[]).map((k) => (
              <option key={k} value={k}>
                {KIND_LABEL[k]}
              </option>
            ))}
          </select>
        </div>
        {!brokerKind && (
          <div className="cm-field">
            <label htmlFor={`${uid}-target`}>{TARGET_LABEL[kind]}</label>
            <input id={`${uid}-target`} className="cm-input" value={target} maxLength={255} disabled={busy}
              placeholder={TARGET_PLACEHOLDER[kind]} spellCheck={false} autoCapitalize="none"
              onChange={(e) => setTarget(e.target.value)} aria-describedby={`${uid}-target-hint`} />
            <p className="cm-field-hint" id={`${uid}-target-hint`}>
              <code>*</code> for all, or a pattern such as <code>reports.*</code>. Each match is its own incident.
            </p>
          </div>
        )}
        {PARAM_FIELDS[kind].map((field) => (
          <div className="cm-field" key={`${kind}-${field.key}`}>
            <label htmlFor={`${uid}-${field.key}`}>{field.label}</label>
            <input
              id={`${uid}-${field.key}`}
              className="cm-input"
              type="number"
              inputMode="decimal"
              min={field.min}
              max={field.max}
              value={params[field.key] ?? ""}
              disabled={busy}
              onChange={(e) => setParams((p) => ({ ...p, [field.key]: e.target.value }))}
              aria-describedby={`${uid}-${field.key}-hint`}
            />
            <p className="cm-field-hint" id={`${uid}-${field.key}-hint`}>
              {field.hint}
            </p>
          </div>
        ))}
        <div className="cm-field">
          <label htmlFor={`${uid}-quiet`}>Quiet period (minutes)</label>
          <input id={`${uid}-quiet`} className="cm-input" type="number" min={0} max={10080} value={quiet}
            disabled={busy} onChange={(e) => setQuiet(e.target.value)} aria-describedby={`${uid}-quiet-hint`} />
          <p className="cm-field-hint" id={`${uid}-quiet-hint`}>
            After a message, the same problem coming back within this time is held back. One incident, one message.
          </p>
        </div>
        <fieldset className="cm-modal-choice" disabled={busy}>
          <legend className="cm-alerts-legend">Send to</legend>
          {channels.length === 0 ? (
            <p className="cm-field-hint">No channels yet. The rule still shows its state here; add a channel to be told.</p>
          ) : (
            channels.map((channel) => (
              <label key={channel.id}>
                <span className="cm-checkbox">
                  <input type="checkbox" checked={channelIds.includes(channel.id)} onChange={() => toggleChannel(channel.id)} />
                  <IconCheck />
                </span>
                {channel.name} <span className="cm-alerts-muted">({CHANNEL_LABEL[channel.kind]})</span>
              </label>
            ))
          )}
        </fieldset>
        <fieldset className="cm-modal-choice" disabled={busy}>
          <legend className="cm-visually-hidden">Options</legend>
          <label>
            <span className="cm-checkbox">
              <input type="checkbox" checked={notifyResolved} onChange={(e) => setNotifyResolved(e.target.checked)} />
              <IconCheck />
            </span>
            Also send a message when it resolves
          </label>
          <label>
            <span className="cm-checkbox">
              <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
              <IconCheck />
            </span>
            Enabled
          </label>
        </fieldset>
        {error && (
          <p className="cm-error" role="alert">
            {error}
          </p>
        )}
        <div className="cm-modal-actions">
          <button type="button" className="cm-btn cm-btn-outline" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="submit" className="cm-btn cm-btn-primary" disabled={!canSubmit}>
            {busy ? "Saving…" : rule ? "Save rule" : "Add rule"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

function ChannelDialog({
  channel,
  onClose,
  onSaved,
}: {
  channel: AlertChannel | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const uid = useId();
  const editing = channel !== null;
  const [name, setName] = useState(channel?.name ?? "");
  const [kind, setKind] = useState<ChannelKind>(channel?.kind ?? "slack");
  const [url, setUrl] = useState("");
  const [secret, setSecret] = useState("");
  const [removeSecret, setRemoveSecret] = useState(false);
  const [to, setTo] = useState((channel?.config.to ?? []).join(", "));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Creating needs a URL; editing may leave it blank to keep the stored one.
  const needsUrl = kind !== "email" && !editing;
  const canSubmit =
    !busy && name.trim() !== "" && (kind === "email" ? to.trim() !== "" : !needsUrl || url.trim() !== "");

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!canSubmit) return;
    const config: ChannelInput["config"] = {};
    if (kind === "email") {
      config.to = to.split(/[\s,;]+/).filter(Boolean);
    } else {
      if (url.trim()) config.url = url.trim();
      if (kind === "webhook") {
        if (removeSecret) config.secret = "";
        else if (secret) config.secret = secret;
      }
    }
    setBusy(true);
    setError(null);
    try {
      if (channel) await updateChannel(channel.id, { name: name.trim(), config });
      else await createChannel({ name: name.trim(), kind, config });
      onSaved();
    } catch (err) {
      setError(errorText(err, "Couldn't save the channel."));
      setBusy(false);
    }
  }

  return (
    <Modal
      title={channel ? `Edit ${channel.name}` : "Add channel"}
      description="Where alert messages go. URLs and secrets are never shown again after saving."
      busy={busy}
      onClose={onClose}
    >
      <form className="cm-modal-body" onSubmit={submit} noValidate>
        <div className="cm-field">
          <label htmlFor={`${uid}-name`}>Name</label>
          <input id={`${uid}-name`} className="cm-input" value={name} maxLength={100} disabled={busy}
            onChange={(e) => setName(e.target.value)} autoComplete="off" />
        </div>
        <div className="cm-field">
          <label htmlFor={`${uid}-kind`}>Type</label>
          <select id={`${uid}-kind`} className="cm-select" value={kind} disabled={busy || editing}
            onChange={(e) => setKind(e.target.value as ChannelKind)}>
            {(Object.keys(CHANNEL_LABEL) as ChannelKind[]).map((k) => (
              <option key={k} value={k}>
                {CHANNEL_LABEL[k]}
              </option>
            ))}
          </select>
        </div>
        {kind === "email" ? (
          <div className="cm-field">
            <label htmlFor={`${uid}-to`}>Recipients</label>
            <textarea id={`${uid}-to`} className="cm-input cm-alerts-textarea" rows={3} value={to} disabled={busy}
              onChange={(e) => setTo(e.target.value)} spellCheck={false} aria-describedby={`${uid}-to-hint`} />
            <p className="cm-field-hint" id={`${uid}-to-hint`}>
              Separate addresses with commas. The SMTP server is set in the server&rsquo;s configuration.
            </p>
          </div>
        ) : (
          <div className="cm-field">
            <label htmlFor={`${uid}-url`}>{kind === "slack" ? "Slack incoming webhook URL" : "Webhook URL"}</label>
            <input id={`${uid}-url`} className="cm-input" type="url" value={url} disabled={busy} spellCheck={false}
              autoComplete="off" onChange={(e) => setUrl(e.target.value)}
              placeholder={editing ? `Unchanged: ${channel?.config.url ?? ""}` : "https://"}
              aria-describedby={`${uid}-url-hint`} />
            <p className="cm-field-hint" id={`${uid}-url-hint`}>
              {editing ? "Leave blank to keep the current URL. " : ""}Must start with https:// (or http://).
            </p>
          </div>
        )}
        {kind === "webhook" && (
          <div className="cm-field">
            <label htmlFor={`${uid}-secret`}>Signing secret (optional)</label>
            <input id={`${uid}-secret`} className="cm-input" type="password" value={secret} autoComplete="new-password"
              disabled={busy || removeSecret} onChange={(e) => setSecret(e.target.value)}
              placeholder={channel?.config.secret_set ? "Unchanged" : ""} aria-describedby={`${uid}-secret-hint`} />
            <p className="cm-field-hint" id={`${uid}-secret-hint`}>
              Requests then carry an <code>X-Celery-Manager-Signature</code> HMAC-SHA256 header.
            </p>
            {channel?.config.secret_set && (
              <label className="cm-alerts-inline-check">
                <span className="cm-checkbox">
                  <input type="checkbox" checked={removeSecret} disabled={busy}
                    onChange={(e) => setRemoveSecret(e.target.checked)} />
                  <IconCheck />
                </span>
                Remove the signing secret
              </label>
            )}
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
          <button type="submit" className="cm-btn cm-btn-primary" disabled={!canSubmit}>
            {busy ? "Saving…" : channel ? "Save channel" : "Add channel"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

// --- page --------------------------------------------------------------------------

function Loading({ what }: { what: string }) {
  return (
    <div className="cm-card-pad">
      <p className="cm-loading">
        <span className="cm-live-dot" aria-hidden="true" />
        Loading {what}&hellip;
      </p>
    </div>
  );
}

function LoadError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="cm-card-pad cm-stack">
      <p className="cm-error" role="alert">
        {message}
      </p>
      <button type="button" className="cm-btn cm-btn-outline" onClick={onRetry}>
        Try again
      </button>
    </div>
  );
}

export function Alerts() {
  const isAdmin = useCan("admin");
  const [rules, setRules] = useState<AlertRule[] | null>(null);
  const [channels, setChannels] = useState<AlertChannel[] | null>(null);
  const [incidents, setIncidents] = useState<AlertIncident[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  // A refused row action, in the server's words.
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [editingRule, setEditingRule] = useState<AlertRule | "new" | null>(null);
  const [editingChannel, setEditingChannel] = useState<AlertChannel | "new" | null>(null);

  const refresh = useCallback(() => {
    setLoadError(null);
    Promise.all([listRules(), listChannels(), listIncidents(30)])
      .then(([r, c, i]) => {
        setRules(r);
        setChannels(c);
        setIncidents(i);
      })
      .catch((err) => setLoadError(errorText(err, "Couldn't load alerts.")));
  }, []);

  useEffect(() => {
    refresh();
    // Incidents open and close on the evaluator's schedule, not on anything
    // done here; a slow poll keeps the state honest without a manual reload.
    const timer = window.setInterval(refresh, 30_000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  async function act(key: string, fn: () => Promise<void>, success: string | null, fallback: string) {
    setBusyKey(key);
    setActionError(null);
    setNotice(null);
    try {
      await fn();
      if (success) setNotice(success);
      setConfirming(null);
      refresh();
    } catch (err) {
      setActionError(errorText(err, fallback));
      setConfirming(null);
      refresh();
    } finally {
      setBusyKey(null);
    }
  }

  const channelNames = new Map((channels ?? []).map((c) => [c.id, c.name]));
  const unchecked = (rules ?? []).some((r) => r.enabled && !r.last_checked_at);

  return (
    <div className="cm-page">
      <header className="cm-page-header">
        <div className="cm-page-header-titles">
          <h1 className="cm-page-title">Alerts</h1>
          <p className="cm-page-lede">
            A message to Slack, a webhook or email when something goes wrong.
            {!isAdmin && ` Read-only: ${needsRole("admin").toLowerCase()} to change rules and channels.`}
          </p>
        </div>
        {isAdmin && (
          <div className="cm-alerts-header-actions">
            <button type="button" className="cm-btn cm-btn-outline" onClick={() => setEditingChannel("new")}>
              Add channel
            </button>
            <button type="button" className="cm-btn cm-btn-primary" onClick={() => setEditingRule("new")}>
              Add rule
            </button>
          </div>
        )}
      </header>

      {actionError && (
        <p className="cm-error cm-alerts-banner" role="alert">
          {actionError}
        </p>
      )}
      {notice && (
        <p className="cm-notice cm-alerts-banner" role="status">
          {notice}
        </p>
      )}
      {unchecked && (
        <p className="cm-alert cm-alerts-banner" role="status">
          Some rules have not been checked yet. Rules are judged by a separate process: run{" "}
          <code>celery-manager alerts</code>, or the events listener with <code>--with-alerts</code>.
        </p>
      )}

      <section className="cm-card cm-card-flush" aria-labelledby="cm-alerts-rules">
        <div className="cm-card-pad cm-card-header">
          <h2 className="cm-card-title" id="cm-alerts-rules">
            Rules
          </h2>
        </div>
        {loadError && rules === null ? (
          <LoadError message={loadError} onRetry={refresh} />
        ) : rules === null ? (
          <Loading what="rules" />
        ) : rules.length === 0 ? (
          <p className="cm-empty-state">
            No alert rules yet.{isAdmin ? " Add one to be told when tasks fail, queues back up, workers go offline or schedules miss a run." : ""}
          </p>
        ) : (
          <div className="cm-table-scroll">
            <table className="cm-table cm-table-stack">
              <thead>
                <tr>
                  <th scope="col">Rule</th>
                  <th scope="col">Condition</th>
                  <th scope="col">Channels</th>
                  <th scope="col" className="cm-col-fit">State</th>
                  {isAdmin && (
                    <th scope="col" className="cm-col-fit">
                      <span className="cm-visually-hidden">Actions</span>
                    </th>
                  )}
                </tr>
              </thead>
              <tbody>
                {rules.map((rule) => {
                  const key = `rule-${rule.id}`;
                  const busy = busyKey === key;
                  return (
                    <tr key={rule.id}>
                      <td className="cm-cell-primary">
                        {rule.name}
                        <span className="cm-alerts-sub">
                          {KIND_LABEL[rule.kind]} · {rule.target === "*" ? "all" : rule.target}
                        </span>
                      </td>
                      <td>
                        {describeCondition(rule)}
                        <span className="cm-alerts-sub">Quiet period {rule.quiet_period_minutes} min</span>
                      </td>
                      <td>
                        {rule.channel_ids.length === 0
                          ? <span className="cm-alerts-muted">None (dashboard only)</span>
                          : rule.channel_ids.map((id) => channelNames.get(id) ?? `#${id}`).join(", ")}
                      </td>
                      <td className="cm-col-fit">
                        <RuleState rule={rule} />
                        {rule.last_check_error && (
                          <span className="cm-alerts-sub cm-alerts-warn" title={rule.last_check_error}>
                            Couldn&rsquo;t check: {rule.last_check_error}
                          </span>
                        )}
                      </td>
                      {isAdmin && (
                        <td className="cm-col-fit">
                          <div className="cm-alerts-actions">
                            {confirming === key ? (
                              <>
                                <span className="cm-alerts-muted">Delete {rule.name}?</span>
                                <button type="button" className="cm-btn cm-btn-destructive cm-btn-sm" disabled={busy}
                                  onClick={() => void act(key, () => deleteRule(rule.id), null, "Couldn't delete the rule.")}>
                                  {busy ? "Deleting…" : "Delete"}
                                </button>
                                <button type="button" className="cm-btn cm-btn-outline cm-btn-sm" disabled={busy}
                                  onClick={() => setConfirming(null)}>
                                  Cancel
                                </button>
                              </>
                            ) : (
                              <>
                                <button type="button" className="cm-btn cm-btn-outline cm-btn-sm"
                                  onClick={() => setEditingRule(rule)}>
                                  Edit
                                </button>
                                <button type="button" className="cm-btn cm-btn-destructive-outline cm-btn-sm"
                                  onClick={() => setConfirming(key)}>
                                  Delete
                                </button>
                              </>
                            )}
                          </div>
                        </td>
                      )}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="cm-card cm-card-flush" aria-labelledby="cm-alerts-channels">
        <div className="cm-card-pad cm-card-header">
          <h2 className="cm-card-title" id="cm-alerts-channels">
            Channels
          </h2>
        </div>
        {loadError && channels === null ? (
          <LoadError message={loadError} onRetry={refresh} />
        ) : channels === null ? (
          <Loading what="channels" />
        ) : channels.length === 0 ? (
          <p className="cm-empty-state">No channels yet.{isAdmin ? " Add a Slack webhook, a webhook or an email list." : ""}</p>
        ) : (
          <div className="cm-table-scroll">
            <table className="cm-table cm-table-stack">
              <thead>
                <tr>
                  <th scope="col">Channel</th>
                  <th scope="col">Sends to</th>
                  <th scope="col">Last delivery</th>
                  {isAdmin && (
                    <th scope="col" className="cm-col-fit">
                      <span className="cm-visually-hidden">Actions</span>
                    </th>
                  )}
                </tr>
              </thead>
              <tbody>
                {channels.map((channel) => {
                  const key = `channel-${channel.id}`;
                  const testKey = `test-${channel.id}`;
                  const busy = busyKey === key || busyKey === testKey;
                  // Whichever happened last decides how the channel reads.
                  const failedLast =
                    channel.last_error !== null &&
                    (!channel.last_sent_at || (channel.last_error_at ?? "") >= channel.last_sent_at);
                  return (
                    <tr key={channel.id}>
                      <td className="cm-cell-primary">
                        {channel.name}
                        <span className="cm-alerts-sub">
                          {CHANNEL_LABEL[channel.kind]}
                          {channel.rule_count !== undefined && ` · used by ${channel.rule_count} rule${channel.rule_count === 1 ? "" : "s"}`}
                        </span>
                      </td>
                      <td className="cm-alerts-dest">{channelDestination(channel)}</td>
                      <td>
                        {failedLast ? (
                          <span className="cm-alerts-warn">
                            Failed {formatWhen(channel.last_error_at)}: {channel.last_error}
                          </span>
                        ) : channel.last_sent_at ? (
                          `Sent ${formatWhen(channel.last_sent_at)}`
                        ) : (
                          <span className="cm-alerts-muted">Nothing sent yet</span>
                        )}
                      </td>
                      {isAdmin && (
                        <td className="cm-col-fit">
                          <div className="cm-alerts-actions">
                            {confirming === key ? (
                              <>
                                <span className="cm-alerts-muted">Delete {channel.name}?</span>
                                <button type="button" className="cm-btn cm-btn-destructive cm-btn-sm" disabled={busy}
                                  onClick={() => void act(key, () => deleteChannel(channel.id), null, "Couldn't delete the channel.")}>
                                  {busyKey === key ? "Deleting…" : "Delete"}
                                </button>
                                <button type="button" className="cm-btn cm-btn-outline cm-btn-sm" disabled={busy}
                                  onClick={() => setConfirming(null)}>
                                  Cancel
                                </button>
                              </>
                            ) : (
                              <>
                                <button type="button" className="cm-btn cm-btn-outline cm-btn-sm" disabled={busy}
                                  onClick={() => void act(testKey, () => testChannel(channel.id),
                                    `Test message sent to ${channel.name}.`, "The test message was not delivered.")}>
                                  {busyKey === testKey ? "Sending…" : "Send test"}
                                </button>
                                <button type="button" className="cm-btn cm-btn-outline cm-btn-sm" disabled={busy}
                                  onClick={() => setEditingChannel(channel)}>
                                  Edit
                                </button>
                                <button type="button" className="cm-btn cm-btn-destructive-outline cm-btn-sm" disabled={busy}
                                  onClick={() => setConfirming(key)}>
                                  Delete
                                </button>
                              </>
                            )}
                          </div>
                        </td>
                      )}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="cm-card cm-card-flush" aria-labelledby="cm-alerts-incidents">
        <div className="cm-card-pad cm-card-header">
          <h2 className="cm-card-title" id="cm-alerts-incidents">
            Recent incidents
          </h2>
        </div>
        {loadError && incidents === null ? (
          <LoadError message={loadError} onRetry={refresh} />
        ) : incidents === null ? (
          <Loading what="incidents" />
        ) : incidents.length === 0 ? (
          <p className="cm-empty-state">No incidents. Each time a rule fires it is listed here.</p>
        ) : (
          <div className="cm-table-scroll">
            <table className="cm-table cm-table-stack">
              <thead>
                <tr>
                  <th scope="col">Rule</th>
                  <th scope="col">For</th>
                  <th scope="col" className="cm-col-fit">State</th>
                  <th scope="col" className="cm-col-fit">Opened</th>
                  <th scope="col" className="cm-col-fit">Resolved</th>
                </tr>
              </thead>
              <tbody>
                {incidents.map((incident) => (
                  <tr key={incident.id}>
                    <td className="cm-cell-primary">
                      {incident.rule_name ?? `#${incident.rule_id}`}
                      {incident.rule_kind && <span className="cm-alerts-sub">{KIND_LABEL[incident.rule_kind]}</span>}
                    </td>
                    <td className="cm-alerts-dest">
                      {incident.key}
                      {incident.delivery_error && (
                        <span className="cm-alerts-sub cm-alerts-warn">Delivery failed: {incident.delivery_error}</span>
                      )}
                    </td>
                    <td className="cm-col-fit">
                      <span className="cm-alert-state" data-state={incident.state}>
                        {incident.state === "firing" ? "Firing" : "Resolved"}
                      </span>
                      {incident.state === "firing" && !incident.notified_at && (
                        <span className="cm-alerts-sub" title="Held back by the quiet period, or every channel failed">
                          Not sent yet
                        </span>
                      )}
                    </td>
                    <td className="cm-col-fit">{formatWhen(incident.opened_at)}</td>
                    <td className="cm-col-fit">{formatWhen(incident.resolved_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {editingRule && channels && (
        <RuleDialog
          rule={editingRule === "new" ? null : editingRule}
          channels={channels}
          onClose={() => setEditingRule(null)}
          onSaved={() => {
            setEditingRule(null);
            setActionError(null);
            refresh();
          }}
        />
      )}
      {editingChannel && (
        <ChannelDialog
          channel={editingChannel === "new" ? null : editingChannel}
          onClose={() => setEditingChannel(null)}
          onSaved={() => {
            setEditingChannel(null);
            setActionError(null);
            refresh();
          }}
        />
      )}
    </div>
  );
}
