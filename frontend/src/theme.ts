import { useSyncExternalStore } from "react";

/**
 * Theme preference — light, dark, or follow the OS.
 *
 * Deliberately *not* a server setting. Which theme a person wants is a
 * property of the person and the screen they're sitting in front of (the same
 * operator wants dark at 3am on a laptop and light on a projector), not of the
 * deployment — so it lives in this browser's localStorage and never touches
 * the API. Branding, which everyone shares, is the opposite and lives in
 * src/branding.tsx. The settings screen states both scopes out loud, because
 * the two controls sit next to each other and look equally personal.
 *
 * How a preference reaches the pixels:
 *
 *   "system"  -> no `data-theme` attribute; tokens.css falls through to
 *                `@media (prefers-color-scheme: dark)`.
 *   "light"   -> `data-theme="light"`, which the media block excludes.
 *   "dark"    -> `data-theme="dark"`, which wins on a light-set OS.
 *
 * The very first paint is handled by the inline script in index.html, not by
 * this module — see the comment there.
 */
export type ThemePreference = "system" | "light" | "dark";

/** What "system" actually resolves to right now. */
export type ResolvedTheme = "light" | "dark";

/**
 * Shared with the bootstrap script in index.html. Renaming this key without
 * renaming it there means the first paint uses the old key, sees nothing, and
 * flashes the wrong theme before this module corrects it.
 */
export const THEME_STORAGE_KEY = "cm-theme";

export const THEME_PREFERENCES: readonly ThemePreference[] = ["system", "light", "dark"];

/** Labels for the switch; kept next to the values so the two can't drift. */
export const THEME_LABELS: Record<ThemePreference, string> = {
  system: "Auto",
  light: "Light",
  dark: "Dark",
};

function isPreference(value: unknown): value is ThemePreference {
  return typeof value === "string" && (THEME_PREFERENCES as readonly string[]).includes(value);
}

/**
 * localStorage access throws outright in some privacy modes (Safari private
 * browsing, storage disabled by policy) rather than returning null. A theme
 * preference is not worth crashing the dashboard over, so every access is
 * guarded and failure means "no preference recorded".
 */
function readStoredPreference(): ThemePreference {
  try {
    const raw = window.localStorage.getItem(THEME_STORAGE_KEY);
    return isPreference(raw) ? raw : "system";
  } catch {
    return "system";
  }
}

function writeStoredPreference(preference: ThemePreference): void {
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, preference);
  } catch {
    // Preference won't survive a reload. The current session still honours it.
  }
}

const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");

/**
 * Write the preference to the document: the attribute tokens.css keys off,
 * plus `color-scheme` so the browser's own furniture (scrollbars, the native
 * colour picker on the settings screen, form controls) is painted to match.
 * The stylesheet sets `color-scheme` too; the meta tag is what covers the gap
 * before the stylesheet loads.
 */
function applyPreference(preference: ThemePreference): void {
  const root = document.documentElement;
  if (preference === "system") {
    root.removeAttribute("data-theme");
  } else {
    root.setAttribute("data-theme", preference);
  }

  const meta = document.querySelector('meta[name="color-scheme"]');
  meta?.setAttribute("content", preference === "system" ? "light dark" : preference);
}

let preference = readStoredPreference();
const listeners = new Set<() => void>();

// Reconcile with whatever the inline bootstrap script did. Normally a no-op;
// it matters when localStorage held a value the script couldn't read, or held
// junk ("Dark", an old value from a previous scheme) that only the validation
// above rejects.
applyPreference(preference);

function emit(): void {
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

// Two tabs of the same dashboard are one browser and should agree. The
// `storage` event only fires in the *other* tabs, so there's no loop here.
window.addEventListener("storage", (event) => {
  if (event.key !== THEME_STORAGE_KEY) return;
  const next = isPreference(event.newValue) ? event.newValue : "system";
  if (next === preference) return;
  preference = next;
  applyPreference(next);
  emit();
});

export function getThemePreference(): ThemePreference {
  return preference;
}

export function setThemePreference(next: ThemePreference): void {
  if (next === preference) return;
  preference = next;
  writeStoredPreference(next);
  applyPreference(next);
  emit();
}

export function getResolvedTheme(): ResolvedTheme {
  if (preference === "system") return darkQuery.matches ? "dark" : "light";
  return preference;
}

function subscribeResolved(listener: () => void): () => void {
  const unsubscribe = subscribe(listener);
  darkQuery.addEventListener("change", listener);
  return () => {
    unsubscribe();
    darkQuery.removeEventListener("change", listener);
  };
}

/**
 * The stored preference. Both snapshot arguments are the same function
 * returning a string primitive, so React never re-renders on an unchanged
 * value and there is no getServerSnapshot mismatch to worry about.
 */
export function useThemePreference(): ThemePreference {
  return useSyncExternalStore(subscribe, getThemePreference, getThemePreference);
}

/**
 * What the page is actually painted as. Tracks the OS media query as well as
 * the stored preference, so a user on "Auto" whose laptop flips to dark at
 * sunset gets a correct answer without a reload — which matters because
 * branding derives its accent differently for a light and a dark surface.
 */
export function useResolvedTheme(): ResolvedTheme {
  return useSyncExternalStore(subscribeResolved, getResolvedTheme, getResolvedTheme);
}
