const BASE = "/api";

export interface AuditEntry {
  username: string;
  action: string;
  target_type: string;
  target_id: string;
  details: string | null;
  created_at: string;
}

export async function listAuditLog(): Promise<AuditEntry[]> {
  const response = await fetch(`${BASE}/audit`, { credentials: "include" });
  if (!response.ok) throw new Error("Couldn't load the audit log.");
  return response.json();
}
