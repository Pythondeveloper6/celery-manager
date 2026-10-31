import { API_BASE as BASE } from "./base";
import { describeFailure } from "./settings";

export type RuleKind =
  | "failure_rate"
  | "queue_backlog"
  | "worker_offline"
  | "missed_run"
  | "broker_unreachable"
  | "broker_memory"
  | "queue_wait"
  | "queue_growing"
  // RabbitMQ
  | "rabbitmq_alarm"
  | "task_failed"
  | "consecutive_failures"
  | "run_stuck"
  | "worker_lost"
  | "beat_stale"
  | "task_sla"
  | "duration_regression"
  | "daily_digest"
  | "run_late"
  | "custom_probe"
  | "scheduler_stale";
export type ChannelKind = "slack" | "webhook" | "email" | "teams";

export interface AlertRule {
  id: number;
  name: string;
  kind: RuleKind;
  target: string;
  params: Record<string, number>;
  channel_ids: number[];
  quiet_period_minutes: number;
  notify_resolved: boolean;
  enabled: boolean;
  created_by: string;
  created_at: string | null;
  updated_at: string | null;
  last_checked_at: string | null;
  last_check_error: string | null;
  state: "ok" | "firing" | "disabled";
  firing_keys: string[];
}

/**
 * As the server returns it: `url` is reduced to scheme and host and the
 * signing secret is never sent back, only whether one is set. Leave a field
 * out of an update to keep what is stored.
 */
export interface AlertChannel {
  id: number;
  name: string;
  kind: ChannelKind;
  config: { url?: string | null; secret_set?: boolean; to?: string[] };
  created_by: string;
  created_at: string | null;
  last_sent_at: string | null;
  last_error: string | null;
  last_error_at: string | null;
  rule_count?: number;
}

export interface AlertIncident {
  id: number;
  rule_id: number;
  rule_name: string | null;
  rule_kind: RuleKind | null;
  key: string;
  state: "firing" | "resolved";
  opened_at: string | null;
  last_seen_at: string | null;
  notified_at: string | null;
  resolved_at: string | null;
  details: Record<string, unknown>;
  delivery_error: string | null;
}

export interface RuleInput {
  name: string;
  kind: RuleKind;
  target: string;
  params: Record<string, number>;
  channel_ids: number[];
  quiet_period_minutes: number;
  notify_resolved: boolean;
  enabled: boolean;
}

export interface ChannelInput {
  name?: string;
  kind?: ChannelKind;
  config?: { url?: string; secret?: string; to?: string[] };
}

// Same contract as api/users.ts: every refusal surfaces the server's own
// sentence (422 bad threshold, 409 duplicate name, 502 a failed test).
async function send<T>(path: string, method: string, body: unknown, fallback: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}/alerts${path}`, {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      credentials: "include",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new Error(method === "GET" ? "Couldn't reach the server." : "Couldn't reach the server. Nothing was changed.");
  }
  if (!response.ok) {
    const message = response.status === 401 ? "Your session has expired. Sign in again." : fallback;
    throw new Error(await describeFailure(response, message));
  }
  return response.json();
}

export async function listRules(): Promise<AlertRule[]> {
  return (await send<{ rules: AlertRule[] }>("/rules", "GET", undefined, "Couldn't load alert rules.")).rules;
}

export function createRule(input: RuleInput): Promise<AlertRule> {
  return send("/rules", "POST", input, "Couldn't create the rule.");
}

export function updateRule(id: number, input: Partial<RuleInput>): Promise<AlertRule> {
  return send(`/rules/${id}`, "PATCH", input, "Couldn't save the rule.");
}

export async function deleteRule(id: number): Promise<void> {
  await send(`/rules/${id}`, "DELETE", undefined, "Couldn't delete the rule.");
}

export async function listChannels(): Promise<AlertChannel[]> {
  return (await send<{ channels: AlertChannel[] }>("/channels", "GET", undefined, "Couldn't load channels.")).channels;
}

export function createChannel(input: ChannelInput): Promise<AlertChannel> {
  return send("/channels", "POST", input, "Couldn't create the channel.");
}

export function updateChannel(id: number, input: ChannelInput): Promise<AlertChannel> {
  return send(`/channels/${id}`, "PATCH", input, "Couldn't save the channel.");
}

export async function deleteChannel(id: number): Promise<void> {
  await send(`/channels/${id}`, "DELETE", undefined, "Couldn't delete the channel.");
}

export async function testChannel(id: number): Promise<void> {
  await send(`/channels/${id}/test`, "POST", undefined, "The test message was not delivered.");
}

export async function listIncidents(limit = 50): Promise<AlertIncident[]> {
  return (await send<{ incidents: AlertIncident[] }>(`/incidents?limit=${limit}`, "GET", undefined, "Couldn't load incidents."))
    .incidents;
}
