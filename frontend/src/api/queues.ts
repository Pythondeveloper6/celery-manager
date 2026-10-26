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
