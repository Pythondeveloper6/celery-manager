import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

/*  The task list's filter state, kept in the URL.
 *
 *  The query string (inside the hash: `#/?status=failed&q=smtp`) is the single
 *  source of truth; every value this hook returns is parsed from it. That is
 *  what makes deep links, reload, and back/forward all work with no extra
 *  code: the browser changes the URL, the hook re-parses, the page follows.
 *
 *  Push vs replace: a discrete choice (status tab, date, grouping, page)
 *  pushes a history entry so Back undoes it. Typing in the search box
 *  *replaces* — one entry per keystroke would make Back useless — and the
 *  write itself is debounced so the URL only learns the term once the API
 *  does.
 *
 *  Defaults are omitted from the URL so a bare `#/` stays a bare `#/`. */

export const STATUS_FILTERS = ["all", "queued", "started", "succeeded", "failed", "retried", "skipped", "revoked", "interrupted"];

export type GroupBy = "none" | "status" | "name" | "date";
const GROUP_VALUES: GroupBy[] = ["none", "status", "name", "date"];

export const PAGE_SIZE = 50;
const SEARCH_DEBOUNCE_MS = 300;

/** The part of the view that a saved view captures. Page position is not part
 *  of a view: "failed SMTP runs" is a question, "page 7 of them" is not. */
export interface TaskViewFilters {
  status: string;
  q: string;
  from: string;
  to: string;
  group: GroupBy;
  mine: string;
}

export const EMPTY_VIEW: TaskViewFilters = { status: "all", q: "", from: "", to: "", group: "none", mine: "" };

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

function parseView(params: URLSearchParams): TaskViewFilters {
  const status = params.get("status") ?? "all";
  const group = params.get("group") as GroupBy;
  const from = params.get("from") ?? "";
  const to = params.get("to") ?? "";
  return {
    status: STATUS_FILTERS.includes(status) ? status : "all",
    q: params.get("q") ?? "",
    mine: params.get("mine") === "1" ? "1" : "",
    from: DATE_RE.test(from) ? from : "",
    to: DATE_RE.test(to) ? to : "",
    group: GROUP_VALUES.includes(group) ? group : "none",
  };
}

/** `null` means live mode; a number is a history-mode offset. */
function parseOffset(params: URLSearchParams): number | null {
  const raw = params.get("offset");
  if (raw === null) return null;
  const n = Math.floor(Number(raw));
  if (!Number.isFinite(n) || n < 0) return null;
  return Math.floor(n / PAGE_SIZE) * PAGE_SIZE;
}

/** Serialise filters to a query string (no leading "?"), omitting defaults. */
export function viewToQuery(view: TaskViewFilters): string {
  const out = new URLSearchParams();
  if (view.status !== "all") out.set("status", view.status);
  if (view.q) out.set("q", view.q);
  if (view.mine) out.set("mine", "1");
  if (view.from) out.set("from", view.from);
  if (view.to) out.set("to", view.to);
  if (view.group !== "none") out.set("group", view.group);
  return out.toString();
}

export function useTaskFilters() {
  const location = useLocation();
  const navigate = useNavigate();

  const params = useMemo(() => new URLSearchParams(location.search), [location.search]);
  const view = useMemo(() => parseView(params), [params]);
  const urlOffset = useMemo(() => parseOffset(params), [params]);

  // Latest params, updated synchronously by `write`, so several setters called
  // in one tick (e.g. clearing every filter) compose instead of each starting
  // from the same stale render.
  const paramsRef = useRef(params);
  useEffect(() => {
    paramsRef.current = params;
  }, [params]);

  const write = useCallback(
    (mutate: (next: URLSearchParams) => void, replace: boolean) => {
      const next = new URLSearchParams(paramsRef.current);
      mutate(next);
      const search = next.toString();
      if (search === paramsRef.current.toString()) return;
      paramsRef.current = next;
      navigate({ pathname: location.pathname, search: search ? `?${search}` : "" }, { replace });
    },
    [navigate, location.pathname]
  );

  // Changing what is being asked invalidates the page position. In history
  // mode that means back to its first page (the mode itself survives, as the
  // list's own comments explain); in live mode there is nothing to reset.
  const setFilter = useCallback(
    (key: keyof TaskViewFilters, value: string, replace = false) => {
      write((next) => {
        const empty = EMPTY_VIEW[key];
        if (value === empty) next.delete(key);
        else next.set(key, value);
        if (key !== "group" && next.has("offset")) next.set("offset", "0");
      }, replace);
    },
    [write]
  );

  // Search box: `searchInput` is what is typed, `view.q` what the URL (and so
  // the API) has. The debounce is the only writer of the latter while typing.
  const [searchInput, setSearchInput] = useState(view.q);
  const lastCommittedQ = useRef(view.q);

  useEffect(() => {
    if (searchInput === lastCommittedQ.current) return;
    const timer = setTimeout(() => {
      lastCommittedQ.current = searchInput;
      setFilter("q", searchInput, true);
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [searchInput, setFilter]);

  // Back/forward (or a pasted link) changed `q` underneath the box.
  useEffect(() => {
    if (view.q !== lastCommittedQ.current) {
      lastCommittedQ.current = view.q;
      setSearchInput(view.q);
    }
  }, [view.q]);

  /** Apply a search immediately (chips, clear button), bypassing the debounce. */
  const setSearch = useCallback(
    (value: string) => {
      lastCommittedQ.current = value;
      setSearchInput(value);
      setFilter("q", value);
    },
    [setFilter]
  );

  /** Replace every filter at once as one history entry (saved views, clear). */
  const applyView = useCallback(
    (next: TaskViewFilters) => {
      lastCommittedQ.current = next.q;
      setSearchInput(next.q);
      write((p) => {
        for (const key of Object.keys(EMPTY_VIEW) as (keyof TaskViewFilters)[]) {
          if (next[key] === EMPTY_VIEW[key]) p.delete(key);
          else p.set(key, next[key]);
        }
        if (p.has("offset")) p.set("offset", "0");
      }, false);
    },
    [write]
  );

  const setStatus = useCallback((v: string) => setFilter("status", v), [setFilter]);
  const setDateFrom = useCallback((v: string) => setFilter("from", v), [setFilter]);
  const setDateTo = useCallback((v: string) => setFilter("to", v), [setFilter]);
  const setGroupBy = useCallback((v: GroupBy) => setFilter("group", v), [setFilter]);
  const clearFilters = useCallback(() => applyView(EMPTY_VIEW), [applyView]);

  /** `null` leaves history mode. Pushes, so Back steps through pages. */
  const setUrlOffset = useCallback(
    (offset: number | null) => {
      write((p) => {
        if (offset === null) p.delete("offset");
        else p.set("offset", String(offset));
      }, false);
    },
    [write]
  );

  return {
    status: view.status,
    mine: view.mine,
    setMine: (value: string) => setFilter("mine", value),
    search: view.q,
    dateFrom: view.from,
    dateTo: view.to,
    groupBy: view.group,
    searchInput,
    setStatus,
    setSearchInput,
    setSearch,
    setDateFrom,
    setDateTo,
    setGroupBy,
    clearFilters,
    applyView,
    view,
    urlOffset,
    setUrlOffset,
  };
}
