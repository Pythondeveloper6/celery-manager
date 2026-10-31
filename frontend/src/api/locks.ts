import { API_BASE as BASE } from "./base";
import { describeFailure } from "./settings";

export interface SingletonLease {
  key: string;
  lease_id: string;
  task_id: string;
  task_name: string;
  acquired_at: string;
  ttl_ms: number;
}

export async function listSingletonLocks(): Promise<SingletonLease[]> {
  const response = await fetch(`${BASE}/locks`, { credentials: "include" });
  if (!response.ok) throw new Error(await describeFailure(response, "Couldn't inspect singleton locks."));
  return (await response.json()).locks;
}

export async function releaseSingletonLock(lease: SingletonLease): Promise<void> {
  const response = await fetch(`${BASE}/locks/release`, {
    method: "POST", credentials: "include", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key: lease.key, lease_id: lease.lease_id }),
  });
  if (!response.ok) throw new Error(await describeFailure(response, "Couldn't release this lock. Refresh before retrying."));
}
