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
  const response = await fetch(`${BASE}/whoami`, { credentials: "include" });
  if (!response.ok) return null;
  return response.json();
}
