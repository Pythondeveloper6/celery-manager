import { API_BASE as BASE } from "./base";

// Two different numbers per queue, never to be merged in the UI:
//   depth           — messages the broker holds right now (null = couldn't ask)
//   recorded_queued — runs celery-manager has recorded as `queued` here
// See control/queues.py for why they routinely disagree.
export interface QueueInfo {
  name: string;
  sources: string[];
  depth: number | null;
  // null on Redis and other kombu virtual transports, which can't report it.
  consumers: number | null;
  exists: boolean | null;
  reason: string | null;
  recorded_queued: number;
  // Queue wait over the listing's wait window (ETA time excluded); null when
  // no run with a recorded send time started in it.
  wait: QueueWait | null;
}

export interface QueueWait {
  runs: number;
  p50: number;
  p95: number;
  max: number;
}

export interface DepthPoint {
  at: string;
  depth: number;
}

export interface DepthHistory {
  queue: string;
  hours: number;
  bucket_seconds: number;
  samples: number;
  points: DepthPoint[];
}

export interface QueueListing {
  broker: {
    transport: string | null;
    error: string | null;
    unsupported: boolean;
    workers_replied: boolean | null;
  };
  queues: QueueInfo[];
  recorded_queued_unassigned: number;
  wait_window_seconds: number;
}

export interface PurgeResult {
  queue: string;
  messages_removed: number;
  runs_marked_revoked: number;
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

export async function listQueues(): Promise<QueueListing> {
  const response = await fetch(`${BASE}/queues`, { credentials: "include" });
  if (!response.ok) throw new Error("Couldn't load queues.");
  return response.json();
}

export async function purgeQueue(name: string): Promise<PurgeResult> {
  const response = await fetch(`${BASE}/queues/${encodeURIComponent(name)}/purge`, {
    method: "POST",
    credentials: "include",
  });
  if (!response.ok) throw new Error(await detail(response, "Purge failed."));
  return response.json();
}

export async function getQueueHistory(name: string, hours: number): Promise<DepthHistory> {
  const response = await fetch(`${BASE}/queues/${encodeURIComponent(name)}/history?hours=${hours}`, {
    credentials: "include",
  });
  if (!response.ok) throw new Error(await detail(response, "Couldn't load the depth history."));
  return response.json();
}

// One message waiting in a queue (GET /api/queues/{name}/messages). Never the
// raw body: args/kwargs are text, cut server-side, and null when the
// deployment turned argument display off (`args_visible: false`).
export interface QueuedMessage {
  position: number;
  task_id: string | null;
  task_name: string | null;
  eta: string | null;
  retries: number | null;
  priority: number | null;
  // Redis priority list the message sits in (0, 3, 6, 9); null on RabbitMQ.
  priority_step: number | null;
  size_bytes: number;
  sent_at: string | null;
  age_seconds: number | null;
  args: string | null;
  kwargs: string | null;
  args_note: string | null;
  // The run celery-manager recorded for this task id, if any.
  run: { status: string } | null;
}

export interface QueueMessages {
  queue: string;
  transport: string | null;
  supported: boolean;
  reason: string | null;
  // RabbitMQ: looking changes the queue (see side_effects).
  approximate: boolean;
  side_effects: string[];
  args_visible: boolean;
  total: number | null;
  offset: number;
  limit: number;
  messages: QueuedMessage[];
}

export async function listQueueMessages(name: string, offset: number, limit: number): Promise<QueueMessages> {
  const params = new URLSearchParams({ offset: String(offset), limit: String(limit) });
  const response = await fetch(`${BASE}/queues/${encodeURIComponent(name)}/messages?${params}`, {
    credentials: "include",
  });
  if (!response.ok) throw new Error(await detail(response, "Couldn't load the messages in this queue."));
  return response.json();
}

function messageUrl(name: string, taskId: string): string {
  return `${BASE}/queues/${encodeURIComponent(name)}/messages/${encodeURIComponent(taskId)}`;
}

export async function deleteQueueMessage(name: string, taskId: string): Promise<void> {
  const response = await fetch(messageUrl(name, taskId), { method: "DELETE", credentials: "include" });
  if (!response.ok) throw new Error(await detail(response, "Couldn't delete the message."));
}

export async function moveQueueMessage(name: string, taskId: string, target: string): Promise<void> {
  const response = await fetch(`${messageUrl(name, taskId)}/move`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ target }),
  });
  if (!response.ok) throw new Error(await detail(response, "Couldn't move the message."));
}
