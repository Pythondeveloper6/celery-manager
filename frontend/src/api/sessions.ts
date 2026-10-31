import { API_BASE as BASE } from "./base";
import { describeFailure } from "./settings";

export interface BrowserSession {
  session_id: string;
  created_at: string;
  expires_at: string;
  current: boolean;
}

export async function listOwnSessions(): Promise<BrowserSession[]> {
  const response = await fetch(`${BASE}/auth/sessions`, { credentials: "include" });
  if (!response.ok) throw new Error(await describeFailure(response, "Couldn't load your sessions."));
  return (await response.json()).sessions;
}

export async function revokeOwnSession(id: string): Promise<void> {
  const response = await fetch(`${BASE}/auth/sessions/${encodeURIComponent(id)}/revoke`, {
    method: "POST", credentials: "include",
  });
  if (!response.ok) throw new Error(await describeFailure(response, "Couldn't end this session."));
}
