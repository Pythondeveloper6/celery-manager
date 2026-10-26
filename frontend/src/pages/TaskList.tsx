import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { bulkRetryTasks, listTasks, subscribeToUpdates, type BulkCancelResult, type TaskSummary } from "../api/tasks";
import { TaskCallDialog } from "../components/TaskCallDialog";
import { CancelMatching, CancelSelected } from "../components/BulkCancel";
import { describeCancel } from "../lib/bulkCancel";
import { RunStrip } from "../components/RunStrip";
import { IconCalendar, IconCheck, IconSearch, IconX } from "../components/icons";
import { StatusBadge } from "../components/Status";
import { TaskViewTools } from "../components/TaskViewTools";
import { statusLabel } from "../lib/status";
import {
  elapsedSeconds,
  formatDuration,
  isRunning,
  progressPercent,
  projectedRemainingSeconds,
  secondsSince,
  useNow,
} from "../lib/taskProgress";
import { PAGE_SIZE, STATUS_FILTERS, useTaskFilters, type GroupBy } from "../lib/useTaskFilters";
import "../styles/tasks.css";

// Mirrors MAX_PAGE_SIZE in control/tasks.py. The API *clamps* an oversized
// limit rather than rejecting it, which means asking for 800 rows silently
// returns 500 — and a client that then rendered "Showing 800 of 1,284" would
// be lying. Respecting the cap here keeps the count honest and keeps the
// "Load more" button from offering a page it can't actually deliver.
const MAX_PAGE_SIZE = 500;
const MAX_PAGES = MAX_PAGE_SIZE / PAGE_SIZE;

// How long a running task may go without reporting progress before the list
// says so. Two minutes: `report_progress()` is called by the task body at
// whatever cadence its author chose, and a chunked job that reports once per
// batch can easily be silent for 30–60s while working perfectly normally —
// flagging that would cry wolf. Two minutes is past the plausible-chunk range
// while still catching a wedged import inside the span of attention someone
// gives a dashboard during an incident.
const STALLED_AFTER_SECONDS = 120;

/** How the list is reading the table, and it is always one or the other.
 *
 *  "live"    — the default. Refetches the whole accumulated span from
 *              offset 0 every time, polls every 5s, grows by "Load more" up
 *              to the API's single-page ceiling. Correct for the newest runs,
 *              which is what a dashboard is for.
 *  "history" — explicit offset paging over the full filtered total, one page
 *              at a time, with the poll switched off. Correct for deliberately
 *              reading old runs, which is the only case the live contract
 *              cannot serve (see the note on `refresh`).
 *
 *  Two modes rather than one clever mode: the live contract's guarantee ("a
 *  background refresh can never collapse loaded history") and deep paging's
 *  requirement ("a stable offset window") are genuinely incompatible while
 *  new rows keep arriving at the head. Rather than half-satisfy both, each
 *  mode is internally consistent and the switch between them is an explicit,
 *  visible, reversible user action — never a silent change of behaviour. */
type ViewMode = "live" | "history";

const GROUP_OPTIONS: { value: GroupBy; label: string }[] = [
  { value: "none", label: "None" },
  { value: "status", label: "Status" },
  { value: "name", label: "Task name" },
  { value: "date", label: "Date" },
];

// Lifecycle order, not alphabetical: what is happening now, then what is
// waiting or being retried, then what broke, and only then the settled and
// administrative states. That is the order someone reads the board in during
// an incident.
const STATUS_ORDER = ["started", "queued", "retried", "failed", "succeeded", "skipped", "revoked"];

// Sentinel bucket key for runs that never started. Must not be able to
// collide with a real day key, which is always "YYYY-MM-DD" — the underscores
// guarantee that. (This was briefly a leading-space string, which a tooling
// round-trip mangled into a literal NUL byte; an explicit, visible sentinel
// cannot be corrupted silently that way.)
const NOT_STARTED_KEY = "__not-started__";

function formatTimestamp(value: string | null): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

/*  Both date helpers below return a **UTC instant** — an ISO string ending in
 *  `Z`, produced by `toISOString()`. That form is required, not stylistic:
 *
 *    - It must not be an offset-bearing local string ("2026-08-14T23:59:59
 *      +02:00"). On SQLite, SQLAlchemy's DateTime bind processor renders such
 *      a value as the literal '2026-08-14 23:59:59.000000' — it *drops* the
 *      offset instead of converting it. Stored timestamps are UTC, so the
 *      comparison then happens wall-clock-to-UTC and the whole range is
 *      silently wrong by the viewer's offset. `toISOString()` converts, so
 *      wall-clock and UTC comparison agree. See the "Send date filters in
 *      UTC" note in docs/rest-api.md.
 *    - It must not be built by hand-formatting local date parts into an ISO
 *      shape, which is the tempting "simplification" and reintroduces exactly
 *      the bug above.
 *
 *  The *moment* is still computed in local time, because a person picking
 *  "Aug 14" means their own calendar day; only the serialization is UTC. */

/** The instant a user means by "from this day".
 *
 *  `<input type="date">` hands back a bare "2026-08-14" with no timezone.
 *  Constructing the Date from parts builds local midnight; `toISOString()`
 *  converts that to the UTC instant the API compares against. Parsing
 *  "2026-08-14" directly would not do this — the Date constructor reads a
 *  bare date string as *UTC* midnight, which for a viewer in UTC+10 quietly
 *  starts the range ten hours early. */
function startOfDayIso(value: string): string | undefined {
  if (!value) return undefined;
  const [year, month, day] = value.split("-").map(Number);
  if (!year || !month || !day) return undefined;
  return new Date(year, month - 1, day, 0, 0, 0, 0).toISOString();
}

/** The instant a user means by "up to and including this day".
 *
 *  The server compares `date_to` as a plain instant (`started_at <= date_to`),
 *  not as a calendar day — see the comment on `_apply_task_filters` in
 *  control/tasks.py — whose comment says "the frontend's date picker does
 *  exactly that", meaning this function. So sending local midnight for
 *  "Aug 14" would match only runs that started in the first millisecond of
 *  the 14th, and the user's own last day would appear to vanish from their
 *  results. End-of-day *in the viewer's timezone*, serialized as a UTC
 *  instant, is what makes the picker mean what it looks like it means. */
function endOfDayIso(value: string): string | undefined {
  if (!value) return undefined;
  const [year, month, day] = value.split("-").map(Number);
  if (!year || !month || !day) return undefined;
  return new Date(year, month - 1, day, 23, 59, 59, 999).toISOString();
}

/** Local calendar-day key for a task's start time, or null if it never
 *  started (or the timestamp is unparseable). Built from local date parts
 *  rather than by slicing the ISO string, so a run at 01:00 local on the 15th
 *  groups under the 15th and not under the 14th's UTC date. */
function dayKey(value: string | null): string | null {
  if (!value) return null;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return null;
  const month = String(parsed.getMonth() + 1).padStart(2, "0");
  const day = String(parsed.getDate()).padStart(2, "0");
  return `${parsed.getFullYear()}-${month}-${day}`;
}

/** "Aug 18" — the compact form used on the active-filter chips, where the
 *  year would be noise next to a label that already says From/To. */
function shortDate(value: string): string {
  const [year, month, day] = value.split("-").map(Number);
  if (!year || !month || !day) return value;
  return new Date(year, month - 1, day).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function dayLabel(key: string): string {
  const [year, month, day] = key.split("-").map(Number);
  return new Date(year, month - 1, day).toLocaleDateString(undefined, {
    weekday: "short",
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

interface TaskGroup {
  key: string;
  label: string;
  tasks: TaskSummary[];
}

/** Split the loaded page into display groups.
 *
 *  Client-side over `tasks` — i.e. over the rows currently loaded, never over
 *  the whole matching history. There is no grouped endpoint and this does not
 *  pretend to be one: with pagination active, a "failed — 12" header means
 *  twelve failures *among the rows on screen*, and the note above the table
 *  says as much whenever more history is still unloaded. Getting that wrong is
 *  the difference between a summary and a lie.
 *
 *  Order within each group is left exactly as the server returned it
 *  (newest first), so grouping only ever reorganises rows — it never
 *  resorts them. */
function groupTasks(tasks: TaskSummary[], groupBy: GroupBy): TaskGroup[] {
  if (groupBy === "none") return [{ key: "all", label: "", tasks }];

  const buckets = new Map<string, TaskSummary[]>();
  for (const task of tasks) {
    let key: string;
    if (groupBy === "status") key = task.status;
    else if (groupBy === "name") key = task.task_name;
    else key = dayKey(task.started_at) ?? NOT_STARTED_KEY;

    const bucket = buckets.get(key);
    if (bucket) bucket.push(task);
    else buckets.set(key, [task]);
  }

  const keys = [...buckets.keys()];
  if (groupBy === "status") {
    // Unknown statuses (a future state this build doesn't know about) sort
    // after the known ones rather than silently landing first.
    keys.sort((a, b) => {
      const ia = STATUS_ORDER.indexOf(a);
      const ib = STATUS_ORDER.indexOf(b);
      if (ia !== ib) return (ia < 0 ? STATUS_ORDER.length : ia) - (ib < 0 ? STATUS_ORDER.length : ib);
      return a.localeCompare(b);
    });
  } else if (groupBy === "name") {
    keys.sort((a, b) => a.localeCompare(b));
  } else {
    // Newest day first, matching the list's own ordering. Never-started runs
    // have no day at all; they are the newest rows by insertion order, so
    // they head the list rather than being buried at the bottom.
    keys.sort((a, b) => {
      if (a === NOT_STARTED_KEY) return -1;
      if (b === NOT_STARTED_KEY) return 1;
      return b.localeCompare(a);
    });
  }

  return keys.map((key) => ({
    key,
    label:
      groupBy === "date"
        ? key === NOT_STARTED_KEY
          ? "Not started"
          : dayLabel(key)
        : groupBy === "status"
          ? statusLabel(key)
          : key,
    tasks: buckets.get(key)!,
  }));
}

/** Runtime cell: how long this run has taken, and — for a running,
 *  instrumented run — how far along it is.
 *
 *  Every derivation comes from lib/taskProgress so the list and the detail
 *  page can never disagree about the same run's percentage or clock. */
function RuntimeCell({ task, now }: { task: TaskSummary; now: number }) {
  const running = isRunning(task);
  const elapsed = elapsedSeconds(task, now);
  const remaining = projectedRemainingSeconds(task, elapsed);

  // `progressFraction` returning null means "not instrumented" or "no
  // denominator" — explicitly NOT 0%. A bar at 0% reads as a stuck task, so
  // those rows get a clock and nothing else. The rounding itself comes from
  // lib/taskProgress, so this cell and the detail page can't quote two
  // different percentages for the same run (they used to, at 99.5%+).
  const percent = progressPercent(task);

  const quietFor = running ? secondsSince(task.progress_updated_at, now) : null;
  const stalled = percent !== null && quietFor !== null && quietFor >= STALLED_AFTER_SECONDS;

  const meta: string[] = [];
  if (percent !== null) meta.push(`${percent}%`);
  if (stalled) {
    // The straight-line ETA is extrapolated from a rate that has visibly
    // stopped, so it would be fiction here. Say what is actually known.
    meta.push(`no update for ${formatDuration(quietFor)}`);
  } else if (remaining !== null) {
    meta.push(`~${formatDuration(remaining)} left`);
  }
  if (task.progress_message) meta.push(task.progress_message);
  // Spoken as one comma-separated phrase; shown as separate spans with space
  // between them rather than a " · "-joined string.
  const metaText = meta.join(", ");

  return (
    // The flex box is an inner <div>, NOT the <td> itself. Setting
    // `display: flex` on a table cell overrides `display: table-cell` and
    // drops the cell out of the table formatting context, so the browser
    // wraps it in an anonymous cell — its border and vertical alignment then
    // stop matching the rest of the row. Visible as staggered row rules once
    // the table is wide.
    <td className="cm-col-fit">
      <div className="cm-run-cell-inner">
        <span className="cm-run-clock" data-running={running ? "true" : undefined}>
          {formatDuration(elapsed)}
        </span>

        {running && percent !== null && (
          <div
            className="cm-run-lane"
            data-stalled={stalled ? "true" : undefined}
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={percent}
            // The visible meta line is clipped when it's long; the value text
            // is not, so a screen reader gets the whole progress message.
            aria-valuetext={metaText}
            aria-label={`Progress of ${task.task_name}`}
          >
            <span className="cm-run-lane-fill" style={{ width: `${percent}%` }} />
          </div>
        )}

        {/* Rendered whenever there is something true to say — including for a
            run reporting an open-ended message with no total, which gets the
            message without a (meaningless) bar.

            Hidden from assistive tech *only* when the bar above is present,
            because that bar's aria-valuetext already carries this exact
            string — verified against a real accessibility tree, where the
            cell's name came out as "2m 44s 21% · ~10m 23s left · row 4,182 of
            20,000 21% · ~10m 23s left · row 4,182 of 20,000". With no bar
            (open-ended progress) this span is the only announcement there is,
            so it must stay in the tree. */}
        {running && metaText && (
          <span
            className="cm-run-meta"
            data-stalled={stalled ? "true" : undefined}
            title={metaText}
            aria-hidden={percent !== null ? true : undefined}
          >
            {meta.map((part, i) => (
              <span key={i}>{part}</span>
            ))}
          </span>
        )}
      </div>
    </td>
  );
}

export function TaskList() {
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [total, setTotal] = useState(0);

  // The filters live in the URL (see lib/useTaskFilters): `searchInput` is
  // what the user sees, `search` the debounced value that reaches the API.
  const {
    status, search, dateFrom, dateTo, groupBy, searchInput,
    setStatus, setSearchInput, setSearch, setDateFrom, setDateTo, setGroupBy,
    clearFilters, applyView, view, urlOffset, setUrlOffset,
  } = useTaskFilters();

  // The date range is a popover off the toolbar.
  const [dateOpen, setDateOpen] = useState(false);
  const dateRef = useRef<HTMLDetailsElement>(null);

  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [bulkRetrying, setBulkRetrying] = useState(false);
  const [startOpen, setStartOpen] = useState(false);
  const navigate = useNavigate();
  const [bulkError, setBulkError] = useState<string | null>(null);
  const [bulkNotice, setBulkNotice] = useState<string | null>(null);

  const [pagesLoaded, setPagesLoaded] = useState(1);

  // Live mode is the default: the overwhelmingly common visit is "what is
  // happening right now", not "what happened three weeks ago".
  const [mode, setMode] = useState<ViewMode>(urlOffset === null ? "live" : "history");
  // History mode only. Always a multiple of PAGE_SIZE, so "page N" is a
  // meaningful thing to say to the user.
  const [offset, setOffset] = useState(urlOffset ?? 0);

  // Filters live in a ref as well as in state (the original code did this for
  // `status` alone) so `refresh` can stay a zero-dependency callback. That is
  // what lets the SSE subscription below stay open across every filter change
  // instead of tearing down and reconnecting `EventSource` on each keystroke —
  // which a debounced search box would otherwise do constantly.
  //
  // This effect is declared FIRST on purpose: effects within a component run
  // in declaration order, so the ref is current by the time the fetch effect
  // further down reads it in the same commit.
  const filtersRef = useRef({ status, search, dateFrom, dateTo });
  useEffect(() => {
    filtersRef.current = { status, search, dateFrom, dateTo };
  }, [status, search, dateFrom, dateTo]);

  // How many pages the user has pulled in. Kept in a ref for the same reason
  // as the filters — and mutated *synchronously* by `loadMore` / the filter
  // effect so the very next fetch uses the new span without waiting for a
  // re-render.
  const pagesLoadedRef = useRef(1);

  // Mode and offset follow the same ref-plus-state pattern, and for the same
  // two reasons: `refresh` stays dependency-free (so the EventSource is never
  // torn down), and a mode or page change can set the ref synchronously and
  // fetch immediately instead of waiting a render for the state to land.
  const modeRef = useRef<ViewMode>(urlOffset === null ? "live" : "history");
  const offsetRef = useRef(urlOffset ?? 0);
  const firstFilterRun = useRef(true);

  // Only the very first fetch shows the full "Loading tasks…" takeover;
  // every fetch after that (filter switch or a live SSE event) just dims
  // the panel briefly via `refreshing` so the existing rows never flicker
  // away while fresher data is on the way.
  const hasLoadedRef = useRef(false);

  // Monotonic request id. With a 300ms-debounced search, a 5s poll and SSE
  // refreshes all able to be in flight at once, responses can land out of
  // order; without this an older response would overwrite a newer one and the
  // table would show results for a filter the user has already moved past.
  const requestIdRef = useRef(0);

  const deckRef = useRef<HTMLDivElement>(null);

  // `silent` skips the `refreshing` dim. Background polling (below) uses it
  // so the panel doesn't pulse every few seconds for a fetch the user never
  // asked for; user-initiated and SSE-driven refreshes still show the dim.
  // `more` marks the "Load more" fetch, which gets its own in-button
  // indicator instead of dimming the rows the user is currently reading.
  const refresh = useCallback(async ({ silent = false, more = false } = {}) => {
    if (hasLoadedRef.current && !silent && !more) setRefreshing(true);
    if (more) setLoadingMore(true);

    const requestId = ++requestIdRef.current;
    try {
      const current = filtersRef.current;
      // THE PAGING/POLLING CONTRACT, and the thing most likely to be broken
      // by a later edit.
      //
      // LIVE MODE: every fetch — first load, filter change, SSE event, 5s
      // poll, and "Load more" alike — asks for the *whole accumulated span*
      // from offset 0, never for "the next 50". Two consequences, both
      // deliberate:
      //
      //   1. A background refresh cannot collapse loaded history. If the poll
      //      re-fetched limit=50/offset=0 it would throw away every extra
      //      page the user pulled in, roughly five seconds after they pulled
      //      it. Re-fetching the span means a poll returns everything already
      //      on screen (plus anything new at the head).
      //   2. It sidesteps offset skew. Rows arrive newest-first, so between
      //      loading page one and asking for offset=50, new runs shift the
      //      window and an appending fetch would duplicate or skip rows.
      //      Re-reading from 0 is always internally consistent.
      //
      // The cost is refetching rows we already hold, which the MAX_PAGE_SIZE
      // ceiling bounds — and that ceiling is exactly why history mode exists.
      //
      // HISTORY MODE: one page at a time at an explicit offset, which reaches
      // the whole table instead of stopping at 500. Offset skew is not a
      // problem here for a structural reason, not a lucky one: the poll is
      // switched off in this mode (see the polling effect below), so no
      // refetch happens behind the user's back and the window they are
      // reading only moves when they move it. New runs arriving at the head
      // can still shift rows between two deliberate page clicks — inherent to
      // offset paging over a live table, and acceptable precisely because
      // someone reading old history is not watching the head.
      const historyMode = modeRef.current === "history";
      const limit = historyMode ? PAGE_SIZE : Math.min(pagesLoadedRef.current * PAGE_SIZE, MAX_PAGE_SIZE);
      const page = await listTasks({
        status: current.status === "all" ? undefined : current.status,
        q: current.search || undefined,
        date_from: startOfDayIso(current.dateFrom),
        date_to: endOfDayIso(current.dateTo),
        limit,
        offset: historyMode ? offsetRef.current : 0,
      });
      if (requestId !== requestIdRef.current) return;
      setTasks(page.items);
      setTotal(page.total);
      setError(null);
    } catch (err) {
      if (requestId !== requestIdRef.current) return;
      setError(err instanceof Error ? err.message : "Couldn't load tasks.");
    } finally {
      if (requestId === requestIdRef.current) {
        hasLoadedRef.current = true;
        setLoading(false);
        setRefreshing(false);
        setLoadingMore(false);
      }
    }
  }, []);

  // Any filter change means a different result set, so three things reset
  // together — and they must, because they are the same fact stated three
  // ways: paging goes back to page one (an offset into the old results is
  // meaningless against the new ones), the selection is dropped (the original
  // code did this for a status switch; a search or date change is exactly the
  // same event, and leaving a stale selection behind would let a bulk retry
  // act on rows the user can no longer see), and stale bulk feedback clears.
  //
  // `pagesLoadedRef` / `offsetRef` are set synchronously here rather than
  // left to the re-render, so the fetch on the next line already asks for a
  // single page one instead of the span or offset from the previous filter.
  //
  // The *mode* deliberately survives a filter change. Someone browsing
  // history who narrows the date range is still browsing history; yanking
  // them back to the live head would throw away the thing they were doing.
  // Filters compose with both modes because `total` is already the filtered
  // count, so "Runs 51-100 of 312 matches" stays true.
  //
  // This also serves as the initial load. `groupBy` is deliberately NOT in the
  // dependency list: grouping is a view of the rows already loaded, so
  // changing it must never re-query or drop the user's selection.
  useEffect(() => {
    pagesLoadedRef.current = 1;
    setPagesLoaded(1);
    // The very first run keeps an offset that arrived in the URL (a pasted
    // deep link); later filter changes go back to page one.
    if (firstFilterRun.current) firstFilterRun.current = false;
    else offsetRef.current = 0;
    setOffset(offsetRef.current);
    setSelected(new Set());
    setBulkError(null);
    setBulkNotice(null);
    refresh();
  }, [status, search, dateFrom, dateTo, refresh]);

  // Subscription stays open across mode changes (no EventSource churn); the
  // guard is in the handler. An SSE event is a live-head event, and applying
  // it while the user reads page 11 would move the ground under them.
  useEffect(
    () =>
      subscribeToUpdates(() => {
        if (modeRef.current === "history") return;
        refresh();
      }),
    [refresh]
  );

  // Polling fallback, deliberately *alongside* the SSE subscription above
  // rather than instead of it. The EventBus that /api/stream reads from lives
  // in the API process's memory, but the documented deployment runs the
  // events listener as a separate OS process — which cannot publish into it.
  // So in a real deployment SSE connects and then delivers nothing, and this
  // poll is what actually keeps the list live. When SSE *does* deliver
  // (single-process setups, and once the backlog's cross-process fix lands),
  // both paths simply call the same refresh and the poll is redundant rather
  // than harmful. See plan.md's Backlog, "Cross-process SSE delivery".
  //
  // THE PAUSE POINT: in history mode no interval is created at all — the poll
  // isn't merely ignored, it doesn't exist. That is deliberate. A poll that
  // fired and early-returned would still be a timer someone could later
  // "fix" into refetching; not creating it makes "history mode does not
  // refetch behind your back" a structural property rather than a
  // convention, and that property is exactly what makes offset paging safe.
  // Switching back to live re-runs this effect and restarts the poll.
  useEffect(() => {
    if (mode === "history") return;
    const interval = setInterval(() => refresh({ silent: true }), 5000);
    return () => clearInterval(interval);
  }, [mode, refresh]);

  // A stale selection pointing at a row the list no longer shows is worse
  // than an empty one — a bulk retry must never silently act on a
  // task_id the user can no longer see. The effect above only fires on a
  // filter *change*; it does nothing when the SAME filters' `tasks` array
  // changes shape underneath a live selection — e.g. a background SSE
  // event (`subscribeToUpdates` -> `refresh()`) or the 5s poll moves a
  // selected task out of "failed" without any filter itself changing. So
  // prune `selected` against the current `tasks` id set on every `tasks`
  // change too, not just on filter change — this is what actually
  // guarantees "selected never contains an id that isn't currently
  // visible," which the filter-only reset does not.
  useEffect(() => {
    const visibleIds = new Set(tasks.map((t) => t.task_id));
    setSelected((prev) => {
      let changed = false;
      const next = new Set<string>();
      for (const id of prev) {
        if (visibleIds.has(id)) {
          next.add(id);
        } else {
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [tasks]);

  function loadMore() {
    if (loadingMore || pagesLoadedRef.current >= MAX_PAGES) return;
    pagesLoadedRef.current += 1;
    setPagesLoaded(pagesLoadedRef.current);
    refresh({ more: true });
  }

  // Every mode/page transition below drops the selection explicitly, for the
  // same reason a filter change does: a bulk retry must never be able to act
  // on a run the user can no longer see. The prune-against-`tasks` effect
  // above is the structural backstop that makes the guarantee airtight even
  // if a future navigation path forgets this line — but stating it here keeps
  // the *intent* visible at each call site rather than relying on a side
  // effect three screens away.
  function leaveCurrentRows() {
    setSelected(new Set());
    setBulkError(null);
    setBulkNotice(null);
  }

  // Deliberate page moves scroll the deck back to its top: the pager lives
  // below fifty rows, so without this a "next page" click leaves the reader
  // staring at the bottom of a page they haven't read.
  function scrollDeckToTop() {
    deckRef.current?.scrollIntoView({ block: "start" });
  }

  /** Live -> history. `startAt` is where the user's attention already is:
   *  the ceiling hand-off starts at run 501 (exactly where the live view ran
   *  out), while "browse by page" starts at the first page. */
  function enterHistory(startAt: number) {
    setUrlOffset(startAt);
    modeRef.current = "history";
    offsetRef.current = startAt;
    setMode("history");
    setOffset(startAt);
    leaveCurrentRows();
    refresh();
    scrollDeckToTop();
  }

  /** History -> live. Always returns to the head at page one; resuming
   *  "wherever you were" would mean resuming a stale offset into a table
   *  that has moved on. The poll restarts via the effect's `mode` dep. */
  function backToLive() {
    setUrlOffset(null);
    modeRef.current = "live";
    offsetRef.current = 0;
    pagesLoadedRef.current = 1;
    setMode("live");
    setOffset(0);
    setPagesLoaded(1);
    leaveCurrentRows();
    refresh();
    scrollDeckToTop();
  }

  function goToOffset(next: number) {
    if (next < 0 || next === offsetRef.current) return;
    setUrlOffset(next);
    offsetRef.current = next;
    setOffset(next);
    leaveCurrentRows();
    refresh();
    scrollDeckToTop();
  }

  // Back/forward (or any other external URL change) moved the page position:
  // follow it. Moves made by the functions above have already updated the refs
  // by the time this runs, so they fall straight through.
  useEffect(() => {
    if (urlOffset === null) {
      if (modeRef.current === "live") return;
      modeRef.current = "live";
      offsetRef.current = 0;
      pagesLoadedRef.current = 1;
      setMode("live");
      setOffset(0);
      setPagesLoaded(1);
    } else {
      if (modeRef.current === "history" && offsetRef.current === urlOffset) return;
      modeRef.current = "history";
      offsetRef.current = urlOffset;
      setMode("history");
      setOffset(urlOffset);
    }
    leaveCurrentRows();
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlOffset]);

  function toggle(taskId: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(taskId)) {
        next.delete(taskId);
      } else {
        next.add(taskId);
      }
      return next;
    });
  }

  async function handleBulkRetry() {
    const taskIds = [...selected];
    if (taskIds.length === 0) return;
    setBulkRetrying(true);
    setBulkError(null);
    setBulkNotice(null);
    try {
      const { new_task_ids } = await bulkRetryTasks(taskIds);
      setSelected(new Set());
      setBulkNotice(`Retried ${new_task_ids.length} task${new_task_ids.length === 1 ? "" : "s"} as new runs.`);
      refresh();
    } catch (err) {
      setBulkError(err instanceof Error ? err.message : "Bulk retry failed.");
    } finally {
      setBulkRetrying(false);
    }
  }

  function handleCancelled(result: BulkCancelResult) {
    setSelected(new Set());
    setBulkError(null);
    setBulkNotice(describeCancel(result));
    refresh();
  }

  function handleBulkError(message: string) {
    setBulkNotice(null);
    setBulkError(message);
  }

  // The clock ticks only while something on screen is actually running, so an
  // idle dashboard settles completely instead of re-rendering once a second
  // forever.
  const anyRunning = useMemo(() => tasks.some(isRunning), [tasks]);
  const now = useNow(anyRunning);

  const groups = useMemo(() => groupTasks(tasks, groupBy), [tasks, groupBy]);

  const filtersActive = status !== "all" || search !== "" || dateFrom !== "" || dateTo !== "";
  const dateFilterActive = dateFrom !== "" || dateTo !== "";
  const historyMode = mode === "history";
  const loadedCount = tasks.length;
  const remainingCount = Math.max(0, total - loadedCount);
  // Selection serves two bulk actions: retry on the Failed tab, cancel on
  // the tabs whose runs can still be stopped.
  const cancellableTab = status === "queued" || status === "started" || status === "retried";
  const showSelectColumn = status === "failed" || cancellableTab;
  const columnCount = showSelectColumn ? 5 : 4;
  // Two different reasons the live view stops: more rows exist and can still
  // be fetched, or more rows exist and the API's single-page ceiling is in
  // the way — the second is what history mode exists to answer.
  const canLoadMore = !historyMode && remainingCount > 0 && pagesLoaded < MAX_PAGES;
  const atApiCap = !historyMode && remainingCount > 0 && pagesLoaded >= MAX_PAGES;

  // History-mode position. Stated as run numbers, not just a page number, so
  // paging is orientation rather than a maze: "Runs 501-550 of 1,284" tells
  // you where you are in the whole filtered history at a glance.
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;
  const rangeFrom = total === 0 ? 0 : offset + 1;
  const rangeTo = offset + loadedCount;

  // Every filter that is currently narrowing the list, as removable chips.
  // Built from the *applied* values (debounced `search`, not `searchInput`)
  // so a chip never claims a filter the server hasn't been told about yet.
  const activeFilters = useMemo(() => {
    const chips: { key: string; label: string; value: string; clear: () => void }[] = [];
    if (status !== "all") chips.push({ key: "status", label: "Status", value: statusLabel(status), clear: () => setStatus("all") });
    if (search !== "")
      chips.push({
        key: "search",
        label: "Search",
        value: search,
        clear: () => {
          // Both, and in this order: clearing only the debounced value would
          // leave the text sitting in the box for 300ms and then re-apply it.
          setSearchInput("");
          setSearch("");
        },
      });
    if (dateFrom !== "") chips.push({ key: "from", label: "From", value: shortDate(dateFrom), clear: () => setDateFrom("") });
    if (dateTo !== "") chips.push({ key: "to", label: "To", value: shortDate(dateTo), clear: () => setDateTo("") });
    return chips;
  }, [status, search, dateFrom, dateTo, setStatus, setSearch, setSearchInput, setDateFrom, setDateTo]);

  // A date filter must never be hidden behind a collapsed disclosure the user
  // has forgotten about. Opening it when one becomes active covers the normal
  // case; the active-filter chips above cover the case where they then close
  // it again, so the filter is always visible somewhere.
  useEffect(() => {
    if (dateFilterActive) setDateOpen(true);
  }, [dateFilterActive]);

  // A popover closes the way popovers do: a click anywhere else, or Escape
  // (which hands focus back to the toggle so the keyboard user isn't lost).
  useEffect(() => {
    if (!dateOpen) return;
    const onPointer = (event: PointerEvent) => {
      if (!dateRef.current?.contains(event.target as Node)) setDateOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setDateOpen(false);
      dateRef.current?.querySelector("summary")?.focus();
    };
    document.addEventListener("pointerdown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [dateOpen]);

  const dateSummary =
    dateFrom && dateTo
      ? `${shortDate(dateFrom)} – ${shortDate(dateTo)}`
      : dateFrom
        ? `From ${shortDate(dateFrom)}`
        : dateTo
          ? `Until ${shortDate(dateTo)}`
          : "Date range";

  const stripCaption = historyMode
    ? `The ${loadedCount.toLocaleString()} runs on this page, newest on the right`
    : `Last ${loadedCount.toLocaleString()} run${loadedCount === 1 ? "" : "s"} shown below, newest on the right`;

  return (
    <div className="cm-page">
      <header className="cm-page-header">
        <h1 className="cm-page-title">Task runs</h1>

        <div className="cm-header-actions">
          {/* The mode is legible from the top of the page: the pulsing "Live"
              dot is a claim about the data, and it would be a lie while the
              poll is stopped. */}
          {historyMode ? (
            <div className="cm-live-indicator" title="Live updates are paused while you browse history">
              <span className="cm-paused-dot" aria-hidden="true" />
              Paused
            </div>
          ) : (
            <div className="cm-live-indicator" title="Updates as workers report task events">
              <span className="cm-live-dot" aria-hidden="true" />
              Live
            </div>
          )}
          <button type="button" className="cm-btn cm-btn-primary cm-btn-sm" onClick={() => setStartOpen(true)}>
            Start task
          </button>
        </div>
      </header>

      {startOpen && (
        <TaskCallDialog mode="start" onClose={() => setStartOpen(false)} onDone={(id) => navigate(`/tasks/${id}`)} />
      )}

      {!loading && tasks.length > 0 && <RunStrip tasks={tasks} now={now} caption={stripCaption} />}

      {/* Status first: "show me the failures" is the question people arrive
          with mid-incident. The API gives one filtered total, not a count per
          status, so the tabs carry no numbers rather than invented ones. */}
      <div className="cm-tabs-list cm-status-tabs" role="group" aria-label="Filter by status">
        {STATUS_FILTERS.map((s) => (
          <button
            key={s}
            type="button"
            className="cm-tabs-trigger cm-status-tab"
            data-status={s}
            aria-pressed={s === status}
            onClick={() => setStatus(s)}
          >
            {s === "all" ? "All" : statusLabel(s)}
          </button>
        ))}
      </div>

      <div className="cm-tasks-toolbar">
        <div className="cm-tasks-search">
          <label className="cm-visually-hidden" htmlFor="cm-tasks-search">
            Search runs
          </label>
          <IconSearch className="cm-tasks-search-icon" />
          <input
            id="cm-tasks-search"
            className="cm-input cm-tasks-search-input"
            type="search"
            value={searchInput}
            placeholder="Search task name, ID or arguments"
            aria-describedby="cm-tasks-search-hint"
            onChange={(e) => setSearchInput(e.target.value)}
          />
          {searchInput && (
            <button
              type="button"
              className="cm-tasks-search-clear"
              aria-label="Clear search"
              onClick={() => setSearchInput("")}
            >
              <IconX />
            </button>
          )}
          <p className="cm-visually-hidden" id="cm-tasks-search-hint">
            Matches task name, task ID, and argument values.
          </p>
        </div>

        <div className="cm-tasks-group">
          <label htmlFor="cm-tasks-group">Group by</label>
          <select
            id="cm-tasks-group"
            className="cm-select"
            value={groupBy}
            onChange={(e) => setGroupBy(e.target.value as GroupBy)}
          >
            {GROUP_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>

        {/* <details> rather than a hand-rolled disclosure: it already does
            the keyboard and screen-reader work. */}
        <details
          ref={dateRef}
          className="cm-tasks-dates"
          open={dateOpen}
          onToggle={(e) => setDateOpen((e.currentTarget as HTMLDetailsElement).open)}
        >
          <summary className="cm-tasks-dates-summary" data-active={dateFilterActive ? "true" : undefined}>
            <IconCalendar />
            {dateSummary}
          </summary>
          <div className="cm-tasks-dates-pop">
            <div className="cm-field">
              <label htmlFor="cm-tasks-from">From</label>
              <input
                id="cm-tasks-from"
                className="cm-input"
                type="date"
                value={dateFrom}
                max={dateTo || undefined}
                onChange={(e) => setDateFrom(e.target.value)}
              />
            </div>
            <div className="cm-field">
              <label htmlFor="cm-tasks-to">To</label>
              <input
                id="cm-tasks-to"
                className="cm-input"
                type="date"
                value={dateTo}
                min={dateFrom || undefined}
                onChange={(e) => setDateTo(e.target.value)}
              />
            </div>
            <p className="cm-field-hint">Matches on start time, so runs that never started are excluded.</p>
          </div>
        </details>

        <TaskViewTools
          filters={view}
          onApplyView={applyView}
          exportParams={{
            status: status === "all" ? undefined : status,
            q: search || undefined,
            date_from: startOfDayIso(dateFrom),
            date_to: endOfDayIso(dateTo),
          }}
        />
      </div>

      {/* What is narrowing this list, each removable on its own — the
          answer to "why does the list look empty?". */}
      {activeFilters.length > 0 && (
        <div className="cm-tasks-active-row">
          <ul className="cm-tasks-active" aria-label="Active filters">
            {activeFilters.map((chip) => (
              <li key={chip.key}>
                <button
                  type="button"
                  className="cm-tasks-chip"
                  onClick={chip.clear}
                  aria-label={`Remove ${chip.label.toLowerCase()} filter ${chip.value}`}
                >
                  <span className="cm-tasks-chip-label">{chip.label}</span>
                  <span className="cm-tasks-chip-value">{chip.value}</span>
                  <IconX />
                </button>
              </li>
            ))}
          </ul>
          <button type="button" className="cm-link-btn" onClick={clearFilters}>
            Clear filters
          </button>
        </div>
      )}

      <div className="cm-tasks-deck" ref={deckRef}>
        {historyMode && (
          <div className="cm-tasks-mode-bar" role="status">
            <span className="cm-tasks-mode-text">
              <span className="cm-paused-dot" aria-hidden="true" />
              Live updates are paused while you browse history
            </span>
            <button type="button" className="cm-btn cm-btn-primary cm-btn-sm" onClick={backToLive}>
              Back to live
            </button>
          </div>
        )}

        {showSelectColumn && selected.size > 0 && (
          <div className="cm-bulk-bar">
            <span className="cm-bulk-bar-label">
              {selected.size} task{selected.size === 1 ? "" : "s"} selected
            </span>
            {status === "failed" ? (
              <button
                type="button"
                className="cm-btn cm-btn-primary cm-btn-sm"
                onClick={handleBulkRetry}
                disabled={bulkRetrying}
              >
                {bulkRetrying ? "Retrying…" : `Retry ${selected.size} selected`}
              </button>
            ) : (
              <CancelSelected taskIds={[...selected]} onDone={handleCancelled} onError={handleBulkError} />
            )}
          </div>
        )}
        {(status === "all" || cancellableTab) && tasks.length > 0 && (
          <div className="cm-bulk-matching-row">
            <CancelMatching
              filter={{
                status: status === "all" ? undefined : status,
                q: search || undefined,
                date_from: startOfDayIso(dateFrom),
                date_to: endOfDayIso(dateTo),
              }}
              onDone={handleCancelled}
              onError={handleBulkError}
            />
          </div>
        )}
        {bulkNotice && (
          <p className="cm-notice" role="status">
            {bulkNotice}
          </p>
        )}
        {bulkError && (
          <p className="cm-error" role="alert">
            {bulkError}
          </p>
        )}

        {(dateFilterActive || (groupBy !== "none" && (historyMode || remainingCount > 0))) && tasks.length > 0 && (
          <ul className="cm-tasks-notes">
            {dateFilterActive && (
              <li>A date range matches on start time, so runs that never started aren&rsquo;t included.</li>
            )}
            {/* Grouping is client-side over what is loaded, and says so
                rather than letting a group count imply a total. */}
            {groupBy !== "none" && historyMode && (
              <li>
                Groups cover the {loadedCount.toLocaleString()} runs on this page, not all {total.toLocaleString()}{" "}
                matches.
              </li>
            )}
            {groupBy !== "none" && !historyMode && remainingCount > 0 && (
              <li>
                Groups cover the {loadedCount.toLocaleString()} runs loaded here, not all {total.toLocaleString()}{" "}
                matches.
              </li>
            )}
          </ul>
        )}

        <div
          className={`cm-card cm-card-flush cm-tasks-panel${refreshing ? " cm-card-refreshing" : ""}`}
          aria-busy={refreshing || loadingMore}
        >
          {/* A failed *background* poll must not blow the table away — the
              rows on screen are still the last good answer. An error with
              rows behind it is a banner; only an error with nothing behind it
              takes over. */}
          {error && !loading && tasks.length > 0 && (
            <div className="cm-card-pad">
              <p className="cm-error" role="alert">
                {error} Showing the last results loaded;{" "}
                {historyMode ? "use Back to live to resume updates." : "retrying every few seconds."}
              </p>
            </div>
          )}

          {loading ? (
            <div className="cm-card-pad">
              <p className="cm-loading">
                <span className="cm-live-dot" aria-hidden="true" />
                Loading task runs&hellip;
              </p>
            </div>
          ) : error && tasks.length === 0 ? (
            <div className="cm-card-pad cm-stack">
              <p className="cm-error" role="alert">
                {error}
              </p>
              <button type="button" className="cm-btn cm-btn-outline" onClick={() => refresh()}>
                Try again
              </button>
            </div>
          ) : tasks.length === 0 ? (
            // "Nothing matches your filters" and "nothing has ever run" are
            // different problems with different fixes.
            filtersActive ? (
              <div className="cm-tasks-empty">
                <p className="cm-tasks-empty-title">No runs match these filters</p>
                <p className="cm-tasks-empty-body">
                  {dateFilterActive
                    ? "Widen the date range or drop a filter. A date range also excludes runs that never started."
                    : "Drop a filter, or search for something else."}
                </p>
                <button type="button" className="cm-btn cm-btn-outline" onClick={clearFilters}>
                  Clear filters
                </button>
              </div>
            ) : (
              <div className="cm-tasks-empty">
                <p className="cm-tasks-empty-title">No task runs recorded yet</p>
                <p className="cm-tasks-empty-body">
                  Runs appear here as soon as a worker reports one. Start the events listener and dispatch a task to
                  see it.
                </p>
              </div>
            )
          ) : (
            <>
              <div className="cm-table-scroll">
                <table className="cm-table cm-table-stack cm-tasks-table" data-select={showSelectColumn ? "true" : undefined}>
                  <thead>
                    <tr>
                      {showSelectColumn && (
                        <th scope="col" className="cm-select-cell">
                          <span className="cm-visually-hidden">Select</span>
                        </th>
                      )}
                      <th scope="col">Task</th>
                      <th scope="col" className="cm-col-fit">
                        Status
                      </th>
                      <th scope="col" className="cm-col-fit">
                        Runtime
                      </th>
                      <th scope="col" className="cm-col-fit">
                        Started
                      </th>
                    </tr>
                  </thead>

                  {/* One <tbody> per group — real table structure, so the
                      grouping survives for assistive tech. */}
                  {groups.map((group) => (
                    <tbody key={group.key}>
                      {groupBy !== "none" && (
                        <tr className="cm-tasks-group-head">
                          <th scope="colgroup" colSpan={columnCount}>
                            {group.label}
                            <span className="cm-tasks-group-count">{group.tasks.length} loaded</span>
                          </th>
                        </tr>
                      )}
                      {group.tasks.map((t) => (
                        <tr key={t.task_id} data-status={t.status}>
                          {showSelectColumn && (
                            <td className="cm-select-cell">
                              <span className="cm-checkbox">
                                <input
                                  type="checkbox"
                                  checked={selected.has(t.task_id)}
                                  onChange={() => toggle(t.task_id)}
                                  aria-label={`Select ${t.task_name} (${t.task_id}) for bulk actions`}
                                />
                                <IconCheck />
                              </span>
                            </td>
                          )}
                          <td className="cm-cell-primary">
                            {/* The id is cut to its first eight characters
                                here; the full id is in the title, on the
                                detail page, and still searchable. */}
                            <Link to={`/tasks/${t.task_id}`} className="cm-task-link" title={t.task_id}>
                              <span className="cm-task-name">{t.task_name}</span>
                              <span className="cm-task-id">{t.task_id.slice(0, 8)}</span>
                            </Link>
                          </td>
                          <td className="cm-col-fit">
                            <StatusBadge status={t.status} />
                          </td>
                          <RuntimeCell task={t} now={now} />
                          <td className="cm-col-fit cm-timestamp">{formatTimestamp(t.started_at)}</td>
                        </tr>
                      ))}
                    </tbody>
                  ))}
                </table>
              </div>

              <div className="cm-tasks-foot">
                <span className="cm-tasks-count">
                  {historyMode
                    ? `Runs ${rangeFrom.toLocaleString()}–${rangeTo.toLocaleString()} of ${total.toLocaleString()}`
                    : remainingCount > 0
                      ? `Showing ${loadedCount.toLocaleString()} of ${total.toLocaleString()} runs`
                      : `Showing all ${total.toLocaleString()} run${total === 1 ? "" : "s"}`}
                </span>

                {historyMode ? (
                  // "Newer"/"Older" rather than Prev/Next: the list is
                  // newest-first, so direction in time is what the user is
                  // actually navigating.
                  <div className="cm-tasks-pager">
                    <button
                      type="button"
                      className="cm-btn cm-btn-outline cm-btn-sm"
                      onClick={() => goToOffset(Math.max(0, offset - PAGE_SIZE))}
                      disabled={offset === 0 || refreshing}
                    >
                      Newer
                    </button>
                    <span className="cm-tasks-pager-position">
                      Page {currentPage.toLocaleString()} of {pageCount.toLocaleString()}
                    </span>
                    <button
                      type="button"
                      className="cm-btn cm-btn-outline cm-btn-sm"
                      onClick={() => goToOffset(offset + PAGE_SIZE)}
                      disabled={currentPage >= pageCount || refreshing}
                    >
                      Older
                    </button>
                  </div>
                ) : canLoadMore ? (
                  <div className="cm-tasks-more">
                    {total > MAX_PAGE_SIZE && (
                      <button type="button" className="cm-link-btn" onClick={() => enterHistory(0)}>
                        Browse all {total.toLocaleString()} runs by page
                      </button>
                    )}
                    <button
                      type="button"
                      className="cm-btn cm-btn-outline cm-btn-sm"
                      onClick={loadMore}
                      disabled={loadingMore}
                      data-loading={loadingMore || undefined}
                    >
                      {loadingMore ? "Loading…" : `Load ${Math.min(PAGE_SIZE, remainingCount)} more`}
                    </button>
                  </div>
                ) : atApiCap ? (
                  <div className="cm-tasks-more">
                    <p className="cm-tasks-more-note">
                      The live view holds the newest {MAX_PAGE_SIZE.toLocaleString()} matching runs, so it never
                      drops what you&rsquo;ve loaded when it refreshes. Older runs are reachable a page at a time,
                      with live updates paused.
                    </p>
                    <button
                      type="button"
                      className="cm-btn cm-btn-outline cm-btn-sm"
                      onClick={() => enterHistory(MAX_PAGE_SIZE)}
                    >
                      Browse older runs
                    </button>
                  </div>
                ) : null}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
