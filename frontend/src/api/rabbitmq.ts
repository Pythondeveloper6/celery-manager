import { API_BASE as BASE, forbiddenMessage } from "./base";

// RabbitMQ management API data (see control/rabbitmq.py and docs/rabbitmq.md).
// Every read answers `{configured: false, reason}` when the management API
// isn't set up or the broker isn't RabbitMQ; the UI then explains the setup.

export interface NotConfigured {
  configured: false;
  reason: string;
}

// One queue as the management API sees it; attached to each queue of
// GET /api/queues as `rabbitmq` when management is configured.
export interface RabbitQueueInfo {
  type: string; // classic | quorum | stream
  state: string | null;
  node: string | null;
  consumers: number | null;
  messages: number | null;
  messages_ready: number | null;
  messages_unacknowledged: number | null;
  // Messages per second; null until the management API has sampled a rate.
  publish_rate: number | null;
  deliver_rate: number | null;
  max_priority: number | null;
  dead_letter_exchange: string | null;
  dead_letter_routing_key: string | null;
  idle_since: string | null;
}

export interface RabbitListingMeta {
  configured: boolean;
  reason: string | null;
  // Set when configured but the management API couldn't be asked.
  error: string | null;
  // Whether the broker is RabbitMQ at all (null if the broker didn't answer).
  is_rabbitmq: boolean | null;
}

export interface RabbitNode {
  name: string;
  running: boolean | null;
  mem_used: number | null;
  mem_limit: number | null;
  mem_alarm: boolean | null;
  disk_free: number | null;
  disk_free_limit: number | null;
  disk_free_alarm: boolean | null;
  fd_used: number | null;
  fd_total: number | null;
  sockets_used: number | null;
  sockets_total: number | null;
  uptime: number | null;
}

export interface RabbitOverview {
  configured: true;
  vhost: string;
  rabbitmq_version: string | null;
  management_version: string | null;
  cluster_name: string | null;
  messages: number | null;
  messages_ready: number | null;
  messages_unacknowledged: number | null;
  publish_rate: number | null;
  deliver_rate: number | null;
  nodes: RabbitNode[];
  alarms: { ok: boolean; reason: string | null; nodes: string[] };
}

export interface DeadLetterQueue {
  name: string;
  // "dead_letter_target" (some queue's dead-letter exchange routes here) and/or "name_pattern".
  matched_by: string[];
  dead_letter_sources: string[];
  messages: number | null;
  messages_ready: number | null;
  consumers: number | null;
  type: string | null;
}

export interface DeadLetterQueueListing {
  configured: true;
  patterns: string[];
  queues: DeadLetterQueue[];
  warnings: string[];
}

export interface XDeath {
  reason: string | null; // rejected | expired | maxlen | delivery_limit
  queue: string | null;
  exchange: string | null;
  routing_keys: string[];
  count: number;
  time: string | null;
}

export interface DeadLetterMessage {
  // Identifies the message for a later requeue: Celery task id, else AMQP message id.
  key: string | null;
  task_name: string | null;
  task_id: string | null;
  // null unless revealed (args_masked says there is something hidden).
  args: string | null;
  kwargs: string | null;
  args_masked: boolean;
  routing_key: string | null;
  exchange: string | null;
  redelivered: boolean | null;
  content_type: string | null;
  payload_bytes: number | null;
  x_death: XDeath[];
  first_death_reason: string | null;
  first_death_queue: string | null;
  death_count: number;
  requeued_by_celery_manager: number;
}

export interface DeadLetterMessages {
  configured: true;
  queue: string;
  messages_ready: number | null;
  // Reading puts messages back, so order and the redelivered flag change.
  approximate: true;
  limit: number;
  args_revealed: boolean;
  messages: DeadLetterMessage[];
}

export interface RequeueResult {
  queue: string;
  moved: number;
  moved_messages: { key: string | null; exchange: string; routing_key: string }[];
  refused: { key: string | null; reason: string }[];
  not_found: string[];
  error: string | null;
  stopped: string | null;
}

async function detail(response: Response, fallback: string): Promise<string> {
  try {
    const body = await response.json();
    if (typeof body?.detail === "string") return body.detail;
  } catch {
    // Not JSON — fall through to the generic message.
  }
  return fallback;
}

async function getJson<T>(path: string, fallback: string): Promise<T | NotConfigured> {
  const response = await fetch(`${BASE}/rabbitmq${path}`, { credentials: "include" });
  if (!response.ok) throw new Error(await detail(response, fallback));
  return response.json();
}

export function getRabbitOverview(): Promise<RabbitOverview | NotConfigured> {
  return getJson("/overview", "Couldn't load RabbitMQ details.");
}

export function listDeadLetterQueues(): Promise<DeadLetterQueueListing | NotConfigured> {
  return getJson("/dlqs", "Couldn't load the dead-letter queues.");
}

export function browseDeadLetterQueue(name: string, revealArgs: boolean): Promise<DeadLetterMessages | NotConfigured> {
  const query = revealArgs ? "?reveal_args=true" : "";
  return getJson(`/dlqs/${encodeURIComponent(name)}/messages${query}`, "Couldn't read the dead-letter queue.");
}

export async function requeueDeadLetters(
  name: string,
  options: { keys?: string[]; limit?: number; force?: boolean },
): Promise<RequeueResult> {
  const response = await fetch(`${BASE}/rabbitmq/dlqs/${encodeURIComponent(name)}/requeue`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(options),
  });
  if (response.status === 403) throw new Error(await forbiddenMessage(response));
  if (!response.ok) throw new Error(await detail(response, "Requeue failed."));
  return response.json();
}
