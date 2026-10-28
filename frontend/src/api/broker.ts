import { API_BASE as BASE } from "./base";

// What GET /api/broker/health returns. `details` depends on the transport:
// Redis has the full memory/clients block, RabbitMQ (AMQP) has server
// properties, everything else is null with `details_note` saying so.
// See control/broker.py.
export interface RedisDetails {
  version: string | null;
  used_memory: number | null;
  // 0 means Redis has no limit.
  maxmemory: number;
  memory_ratio: number | null;
  maxmemory_policy: string | null;
  connected_clients: number | null;
  blocked_clients: number | null;
  evicted_keys: number | null;
  rejected_connections: number | null;
  uptime_seconds: number | null;
  persistence: {
    rdb_last_save_status: string | null;
    rdb_last_save_at: string | null;
    rdb_changes_since_save: number | null;
    aof_enabled: boolean;
    aof_last_write_status: string | null;
  };
  warnings: string[];
}

export interface AmqpDetails {
  product: string | null;
  version: string | null;
  cluster_name: string | null;
  management: string;
  warnings: string[];
}

export interface BrokerHealth {
  transport: string | null;
  reachable: boolean;
  latency_ms: number | null;
  // Credentials already masked by the server.
  url: string | null;
  error: string | null;
  sentinel: boolean;
  details: RedisDetails | AmqpDetails | null;
  details_note: string | null;
  checked_at: string;
}

export async function getBrokerHealth(): Promise<BrokerHealth> {
  const response = await fetch(`${BASE}/broker/health`, { credentials: "include" });
  if (!response.ok) throw new Error("Couldn't load broker health.");
  return response.json();
}
