import { EMPTY_VIEW, type TaskViewFilters } from "./useTaskFilters";

/*  Saved task views, kept per browser in localStorage. No backend involved, so
 *  a view follows the browser profile, not the user account. Every storage
 *  access is wrapped: localStorage can throw (private windows, blocked site
 *  data) or hold something that is no longer JSON, and the page must work
 *  without it. */

const KEY = "cm.taskViews.v1";

export interface SavedView {
  id: string;
  name: string;
  filters: TaskViewFilters;
}

function sanitize(raw: unknown): SavedView[] {
  if (!Array.isArray(raw)) return [];
  const out: SavedView[] = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const { id, name, filters } = item as Partial<SavedView>;
    if (typeof id !== "string" || typeof name !== "string" || !filters || typeof filters !== "object") continue;
    out.push({ id, name, filters: { ...EMPTY_VIEW, ...filters } });
  }
  return out;
}

export function loadViews(): SavedView[] {
  try {
    const text = window.localStorage.getItem(KEY);
    return text ? sanitize(JSON.parse(text)) : [];
  } catch {
    return [];
  }
}

/** Returns false when the browser refused the write, so the UI can say so. */
export function persistViews(views: SavedView[]): boolean {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(views));
    return true;
  } catch {
    return false;
  }
}

export function newViewId(): string {
  return `v${Date.now().toString(36)}${Math.random().toString(36).slice(2, 7)}`;
}
