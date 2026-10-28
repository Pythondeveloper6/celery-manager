import { API_BASE as BASE } from "./base";

/**
 * Deployment-wide branding. Every key is always present — the server overlays
 * stored values on its defaults and returns the full set (see
 * `control/settings.py::get_settings`), so no caller has to handle a missing
 * key or invent its own fallback.
 *
 * An empty string means "use the built-in default" for that key, except
 * `brand_name`, which the server resolves to its own default before storing,
 * so it is never handed back blank.
 */
export interface BrandSettings {
  brand_name: string;
  brand_accent: string;
  brand_logo_url: string;
}

/**
 * What the UI shows before the first response arrives, and what it falls back
 * to if that response never comes. Mirrors `DEFAULT_SETTINGS` in
 * `control/settings.py`; the server is authoritative, this is the offline
 * copy so the login screen can render without waiting for the network.
 */
export const DEFAULT_BRAND_SETTINGS: BrandSettings = {
  brand_name: "celery-manager",
  brand_accent: "",
  brand_logo_url: "",
};

/**
 * Pull the human-readable reason out of a failed response.
 *
 * The settings endpoints answer a rejected value with 422 and
 * `{"detail": "brand_accent must be empty or a hex colour like #0ff or
 * #00ffcc"}` — a message written for the person who typed the value. Replacing
 * that with "Couldn't save settings" would throw away the only part of the
 * response that tells them what to do next, so the server's text is preferred
 * wherever there is one.
 *
 * FastAPI's own request-validation failures use the same status with `detail`
 * as a *list* of objects, which is machine-shaped and useless in a form, so
 * anything that isn't a non-empty string falls back to the generic message.
 */
export async function describeFailure(response: Response, fallback: string): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (body && typeof body === "object" && "detail" in body) {
      const detail = (body as { detail: unknown }).detail;
      if (typeof detail === "string" && detail.trim()) return detail.trim();
    }
  } catch {
    // Not JSON (a proxy error page, an empty body). Fall through.
  }
  return fallback;
}

/**
 * Unauthenticated on purpose: the login screen is branded, and branding that
 * only appears after sign-in is branding nobody sees at the one moment it
 * matters most.
 */
export async function getSettings(): Promise<BrandSettings> {
  let response: Response;
  try {
    response = await fetch(`${BASE}/settings`, { credentials: "include" });
  } catch {
    throw new Error("Couldn't reach the server to load settings.");
  }
  if (!response.ok) {
    throw new Error(await describeFailure(response, "Couldn't load settings."));
  }
  return response.json();
}

/**
 * Update any subset of the branding keys. Returns the full effective settings
 * as the server resolved them (trimmed, defaults substituted), which is what
 * the app should adopt — not the values that were sent.
 */
export async function updateSettings(values: Partial<BrandSettings>): Promise<BrandSettings> {
  let response: Response;
  try {
    response = await fetch(`${BASE}/settings`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify(values),
    });
  } catch {
    throw new Error("Couldn't reach the server. Your changes weren't saved.");
  }
  if (!response.ok) {
    // 401 has its own message: the generic "couldn't save" would send an
    // operator hunting for a typo in a field that was fine.
    const fallback =
      response.status === 401
        ? "Your session has expired. Sign in again to save settings."
        : "Couldn't save settings.";
    throw new Error(await describeFailure(response, fallback));
  }
  return response.json();
}
