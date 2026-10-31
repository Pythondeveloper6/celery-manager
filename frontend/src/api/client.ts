import { API_BASE as BASE } from "./base";

// Roles, least to most privileged. See docs/roles.md; the server enforces
// them, the dashboard only hides what the role can't use.
export type Role = "viewer" | "operator" | "admin";

// "local": an account celery-manager stores itself. "external": someone the
// host app signed in (CeleryManagerConfig.authenticate_request), whose
// password and sign-out belong to the host.
export type AccountSource = "local" | "external";

export interface CurrentUser {
  username: string;
  role: Role;
  source: AccountSource;
  tenant_id?: string;
}

/** How this deployment signs people in: `GET /api/auth/options`. */
export interface AuthOptions {
  environment?: string;
  monitoring_only?: boolean;
  sensitive_actions?: import("../lib/safeguards").SensitiveAction[];
  tenant_scoped?: boolean;
  read_only?: boolean;
  local_login: boolean;
  external: boolean;
  provider_name: string;
  login_url: string | null;
  logout_url: string | null;
}

// What every deployment before host sign-in looked like, and what the login
// screen falls back to if the options can't be fetched: the password form,
// which still reports the server's refusal if local login is in fact off.
export const DEFAULT_AUTH_OPTIONS: AuthOptions = {
  local_login: true,
  external: false,
  provider_name: "your app",
  login_url: null,
  logout_url: null,
};

export async function getAuthOptions(): Promise<AuthOptions> {
  try {
    const response = await fetch(`${BASE}/auth/options`, { credentials: "include" });
    if (!response.ok) return DEFAULT_AUTH_OPTIONS;
    return { ...DEFAULT_AUTH_OPTIONS, ...(await response.json()) };
  } catch {
    return DEFAULT_AUTH_OPTIONS;
  }
}

/** The host app signed this person in but gave them no role here (a 403
 *  from whoami). Not the same as "not signed in": sending them to the login
 *  screen would loop straight back. */
export class NoAccessError extends Error {}

/** `login_url` with `{next}` replaced by this page's address, so the host
 *  can send the person back here after signing in. */
export function hostLoginHref(url: string): string {
  const here = window.location.pathname + window.location.search + window.location.hash;
  return url.split("{next}").join(encodeURIComponent(here));
}

async function detailOf(response: Response): Promise<string | null> {
  try {
    const body = await response.json();
    if (typeof body?.detail === "string" && body.detail.trim()) return body.detail.trim();
  } catch {
    // Not JSON.
  }
  return null;
}

export async function login(username: string, password: string): Promise<CurrentUser> {
  const response = await fetch(`${BASE}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ username, password }),
  });
  if (!response.ok) {
    if (response.status === 401) throw new Error("Invalid username or password");
    if (response.status === 429) {
      const wait = response.headers.get("Retry-After");
      throw new Error(`Too many sign-in attempts. Try again${wait ? ` in ${wait} seconds` : " later"}.`);
    }
    // 403: local sign-in is turned off (or the CSRF check refused it); the
    // server's sentence says which and what to do instead.
    throw new Error((response.status === 403 && (await detailOf(response))) || "Login failed");
  }
  return response.json();
}

export async function whoami(): Promise<CurrentUser | null> {
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
  if (response.status === 403) {
    throw new NoAccessError((await detailOf(response)) ?? "Your account has no access to this dashboard.");
  }
  if (!response.ok) {
    // A 500 is the host's sign-in hook failing; the login screen would hide
    // that behind a form that can't help.
    if (response.status >= 500) {
      throw new Error((await detailOf(response)) ?? "The server couldn't check who you are.");
    }
    return null;
  }
  return response.json();
}

export async function logout(): Promise<void> {
  // Success means the server cleared the session cookie. Anything else —
  // network failure or a non-OK response — leaves the session alive, so it
  // is thrown rather than swallowed: the caller must not pretend the user is
  // signed out when a reload would put them straight back in.
  let response: Response;
  try {
    response = await fetch(`${BASE}/auth/logout`, { method: "POST", credentials: "include" });
  } catch {
    throw new Error("Could not reach the server. You are still signed in.");
  }
  if (!response.ok) throw new Error("Sign out failed. You are still signed in.");
}
