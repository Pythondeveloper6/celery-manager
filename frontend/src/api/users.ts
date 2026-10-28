import { API_BASE as BASE } from "./base";
import type { Role } from "./client";
import { describeFailure } from "./settings";

/**
 * One dashboard account. `source` says where the account is stored: "local"
 * for accounts celery-manager keeps itself. Other values may appear later;
 * treat anything but "local" as read-only here.
 */
export interface DashboardUser {
  username: string;
  role: Role;
  source: string;
}

/** Mirrors `MIN_PASSWORD_LENGTH` in auth/users.py; the server is the authority. */
export const MIN_PASSWORD_LENGTH = 8;

// Every call surfaces the server's own sentence on refusal (a 409 for "only
// admin", a 422 for a short password): it is written for the person who
// clicked, and a generic "failed" would hide what to do next.
async function send<T>(path: string, method: string, body: unknown, fallback: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      method,
      headers: body === undefined ? undefined : { "Content-Type": "application/json" },
      credentials: "include",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new Error("Couldn't reach the server. Nothing was changed.");
  }
  if (!response.ok) {
    const message =
      response.status === 401 ? "Your session has expired. Sign in again." : fallback;
    throw new Error(await describeFailure(response, message));
  }
  return response.json();
}

export async function listUsers(): Promise<DashboardUser[]> {
  const body = await send<{ users: DashboardUser[] }>("/users", "GET", undefined, "Couldn't load users.");
  return body.users;
}

export function createUser(username: string, password: string, role: Role): Promise<DashboardUser> {
  return send("/users", "POST", { username, password, role }, "Couldn't add the user.");
}

export function setUserRole(username: string, role: Role): Promise<DashboardUser> {
  return send(`/users/${encodeURIComponent(username)}`, "PATCH", { role }, "Couldn't change the role.");
}

/** Pass a password, or `generate: true` to get a random one back (shown once). */
export async function resetUserPassword(
  username: string,
  options: { password: string } | { generate: true },
): Promise<string | null> {
  const body = await send<{ generated_password: string | null }>(
    `/users/${encodeURIComponent(username)}/password`,
    "POST",
    options,
    "Couldn't reset the password.",
  );
  return body.generated_password;
}

export async function deleteUser(username: string): Promise<void> {
  await send(`/users/${encodeURIComponent(username)}`, "DELETE", undefined, "Couldn't delete the user.");
}

export async function changeOwnPassword(currentPassword: string, newPassword: string): Promise<void> {
  await send(
    "/me/password",
    "POST",
    { current_password: currentPassword, new_password: newPassword },
    "Couldn't change the password.",
  );
}
