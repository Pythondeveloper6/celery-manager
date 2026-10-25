import { useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { Link } from "react-router-dom";
import type { TaskSummary } from "../api/tasks";
import { elapsedSeconds, formatDuration } from "../lib/taskProgress";
import { statusLabel } from "../lib/status";

// Bar height is log(runtime) on a fixed scale, not one relative to whatever
// happens to be loaded: 0.1s sits at the floor, an hour at the ceiling. A
// fixed scale keeps a 4s run the same height from one visit to the next, so
// the strip's shape means something on its own.
const MIN_PX = 6;
const MAX_PX = 32;
const LOG_FLOOR = Math.log10(0.1);
const LOG_CEIL = Math.log10(3600);

function barHeight(seconds: number | null): number {
  if (seconds === null) return MIN_PX;
  const t = (Math.log10(Math.max(seconds, 0.1)) - LOG_FLOOR) / (LOG_CEIL - LOG_FLOOR);
  return Math.round(MIN_PX + (MAX_PX - MIN_PX) * Math.min(1, Math.max(0, t)));
}

interface Bar {
  task: TaskSummary;
  label: string;
  detail: string;
  height: number;
}

/**
 * The run strip: one thin bar per loaded run, oldest on the left, newest on
 * the right, height by runtime and colour by status. Built purely from the
 * rows the task list already holds — no request of its own.
 *
 * Every bar is a link to its run. With up to 500 of them, they share a single
 * tab stop (roving tabindex): Tab lands on the newest run, arrow keys and
 * Home/End move along the strip, Tab again leaves it.
 */
export function RunStrip({ tasks, now, caption }: { tasks: TaskSummary[]; now: number; caption: string }) {
  const captionId = useId();
  const trackRef = useRef<HTMLDivElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const tipRef = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(true);
  const [focusIndex, setFocusIndex] = useState<number | null>(null);
  const [tip, setTip] = useState<{ index: number; x: number } | null>(null);

  const bars = useMemo<Bar[]>(
    () =>
      [...tasks].reverse().map((task) => {
        const seconds = task.status === "queued" ? null : elapsedSeconds(task, now);
        const duration = seconds === null ? null : formatDuration(seconds);
        const word = statusLabel(task.status);
        return {
          task,
          label: [task.task_name, task.status, duration].filter(Boolean).join(", "),
          detail: duration ? `${word}, ${duration}` : word,
          height: barHeight(seconds),
        };
      }),
    [tasks, now]
  );

  const count = bars.length;
  const tabIndexAt = Math.min(focusIndex ?? count - 1, count - 1);

  // Newest is on the right, so when the strip is wider than its box (many
  // runs, narrow window) it opens scrolled to the right-hand end — and stays
  // there as runs arrive, unless the reader has scrolled back to look.
  useLayoutEffect(() => {
    const track = trackRef.current;
    if (track && pinnedRef.current) track.scrollLeft = track.scrollWidth;
  }, [count]);

  // Keep the tooltip inside the strip's box rather than hanging off the page
  // edge, which would scroll the whole page sideways at 390px.
  useLayoutEffect(() => {
    const el = tipRef.current;
    const wrap = wrapRef.current;
    if (!el || !wrap || !tip) return;
    const max = wrap.clientWidth - el.offsetWidth;
    el.style.left = `${Math.max(0, Math.min(max, tip.x - el.offsetWidth / 2))}px`;
  }, [tip]);

  function show(index: number, el: HTMLElement) {
    const wrap = wrapRef.current;
    if (!wrap) return;
    const bar = el.getBoundingClientRect();
    const box = wrap.getBoundingClientRect();
    setTip({ index, x: bar.left + bar.width / 2 - box.left });
  }

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const moves: Record<string, number> = {
      ArrowLeft: tabIndexAt - 1,
      ArrowRight: tabIndexAt + 1,
      Home: 0,
      End: count - 1,
    };
    if (!(event.key in moves)) return;
    event.preventDefault();
    const next = Math.max(0, Math.min(count - 1, moves[event.key]));
    setFocusIndex(next);
    trackRef.current?.querySelectorAll<HTMLAnchorElement>("a")[next]?.focus();
  }

  if (count === 0) return null;

  const tipBar = tip ? bars[tip.index] : null;

  return (
    <div className="cm-strip" ref={wrapRef}>
      <div
        className="cm-strip-track"
        ref={trackRef}
        role="group"
        aria-label="Recent runs"
        aria-describedby={captionId}
        data-dense={count > 120 ? "true" : undefined}
        onKeyDown={onKeyDown}
        onScroll={(e) => {
          const t = e.currentTarget;
          pinnedRef.current = t.scrollLeft + t.clientWidth >= t.scrollWidth - 4;
          setTip(null);
        }}
        onMouseLeave={() => setTip(null)}
        onBlur={(e) => {
          if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setTip(null);
        }}
      >
        {bars.map((bar, index) => (
          <Link
            key={bar.task.task_id}
            to={`/tasks/${bar.task.task_id}`}
            className="cm-strip-bar"
            data-status={bar.task.status}
            style={{ height: bar.height }}
            aria-label={bar.label}
            tabIndex={index === tabIndexAt ? 0 : -1}
            onMouseEnter={(e) => show(index, e.currentTarget)}
            onFocus={(e) => {
              setFocusIndex(index);
              show(index, e.currentTarget);
            }}
          />
        ))}
      </div>
      {tipBar && (
        <div className="cm-strip-tip" ref={tipRef} aria-hidden="true">
          <span className="cm-strip-tip-name">{tipBar.task.task_name}</span>
          <span className="cm-strip-tip-detail">{tipBar.detail}</span>
        </div>
      )}
      <p className="cm-strip-caption" id={captionId}>
        {caption}
        <span className="cm-visually-hidden">. Use the arrow keys to move between runs.</span>
      </p>
    </div>
  );
}
