const BASE = "/api";

export async function login(username: string, password: string): Promise<{ username: string }> {
  const response = await fetch(`${BASE}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ username, password }),
  });
  if (!response.ok) {
    throw new Error(response.status === 401 ? "Invalid username or password" : "Login failed");
  }
  return response.json();
}

export async function whoami(): Promise<{ username: string } | null> {
  // A clean non-OK response (401) means "confirmed not authenticated" and
  // resolves to null. A network-level failure (server unreachable, DNS,
  // CORS) means "couldn't determine" and is a distinct, thrown error —
  // callers should not treat the two as the same "not signed in" case.
  let response: Response;
  try {
    response = await fetch(`${BASE}/whoami`, { credentials: "include" });
  } catch {
    throw new Error("Could not reach the server. Check your connection and try again.");
  }
  if (!response.ok) return null;
  return response.json();
}
