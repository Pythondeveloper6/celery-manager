import { useEffect, useRef, useState } from "react";
import { pauseSchedule, resumeSchedule, type ScheduleSummary } from "../api/schedules";
import "../styles/schedule-pause.css";

interface Props {
  name: string;
  paused: boolean;
  size?: "sm" | "md";
  // Called with the updated summary after a successful pause or resume.
  onChange: (summary: ScheduleSummary) => void;
  // Called with an error message, or null when a new attempt starts.
  onError: (message: string | null) => void;
}

// `<input type="datetime-local">` reads and writes local "YYYY-MM-DDTHH:mm".
function toLocalInput(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

// 9:00 local tomorrow.
function tomorrowNine(): Date {
  const d = new Date();
  d.setDate(d.getDate() + 1);
  d.setHours(9, 0, 0, 0);
  return d;
}

// Pause / Resume for a whole schedule. Choosing how long to pause in the menu
// is itself the deliberate step (indefinitely, for an hour, until tomorrow
// morning, or until a chosen time); resuming is harmless and fires at once.
export function ScheduleToggle({ name, paused, size = "sm", onChange, onError }: Props) {
  const [open, setOpen] = useState(false);
  const [custom, setCustom] = useState(false);
  const [customValue, setCustomValue] = useState("");
  const [customError, setCustomError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const anchor = useRef<HTMLSpanElement | null>(null);
  const trigger = useRef<HTMLButtonElement | null>(null);
  // Fixed-positioned under the button: the schedules table scrolls
  // horizontally, and an absolutely positioned menu would be clipped by it.
  const [pos, setPos] = useState<{ top: number; right: number } | null>(null);
  const sm = size === "sm" ? " cm-btn-sm" : "";

  useEffect(() => {
    if (!open) return;
    function onPointer(e: MouseEvent) {
      if (anchor.current && !anchor.current.contains(e.target as Node)) close();
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") close();
    }
    document.addEventListener("mousedown", onPointer);
    document.addEventListener("keydown", onKey);
    window.addEventListener("resize", close);
    window.addEventListener("scroll", close, true);
    return () => {
      window.removeEventListener("resize", close);
      window.removeEventListener("scroll", close, true);
      document.removeEventListener("mousedown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  function close() {
    setOpen(false);
    setCustom(false);
    setCustomError(null);
  }

  function openMenu() {
    onError(null);
    const rect = trigger.current?.getBoundingClientRect();
    if (rect) setPos({ top: rect.bottom + 4, right: Math.max(8, window.innerWidth - rect.right) });
    setCustomValue(toLocalInput(new Date(Date.now() + 60 * 60 * 1000)));
    setOpen(true);
  }

  async function run(action: () => Promise<ScheduleSummary>) {
    close();
    onError(null);
    setBusy(true);
    try {
      onChange(await action());
    } catch (err) {
      onError(err instanceof Error ? err.message : "Couldn't update this schedule. Try again.");
    } finally {
      setBusy(false);
    }
  }

  function submitCustom() {
    const when = new Date(customValue);
    if (!customValue || Number.isNaN(when.getTime())) {
      setCustomError("Choose a date and time.");
      return;
    }
    if (when.getTime() <= Date.now()) {
      setCustomError("Choose a time in the future.");
      return;
    }
    run(() => pauseSchedule(name, when));
  }

  if (paused) {
    return (
      <button type="button" className={`cm-btn cm-btn-primary${sm}`} disabled={busy} onClick={() => run(() => resumeSchedule(name))}>
        {busy ? "Resuming…" : "Resume schedule"}
      </button>
    );
  }
  if (busy) {
    return (
      <button type="button" className={`cm-btn cm-btn-outline${sm}`} disabled>
        Pausing…
      </button>
    );
  }
  return (
    <span className="cm-pause-menu-anchor" ref={anchor}>
      <button
        type="button"
        ref={trigger}
        className={`cm-btn cm-btn-outline${sm}`}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => (open ? close() : openMenu())}
      >
        Pause schedule
      </button>
      {open && pos && (
        <div className="cm-pause-menu" role="menu" style={{ top: pos.top, right: pos.right }} aria-label={`Pause ${name}`}>
          <button type="button" role="menuitem" className="cm-pause-menu-item" onClick={() => run(() => pauseSchedule(name))}>
            Pause indefinitely
          </button>
          <button
            type="button"
            role="menuitem"
            className="cm-pause-menu-item"
            onClick={() => run(() => pauseSchedule(name, new Date(Date.now() + 60 * 60 * 1000)))}
          >
            For 1 hour
          </button>
          <button type="button" role="menuitem" className="cm-pause-menu-item" onClick={() => run(() => pauseSchedule(name, tomorrowNine()))}>
            Until tomorrow 9:00
          </button>
          {custom ? (
            <div className="cm-pause-menu-custom">
              <input
                type="datetime-local"
                className="cm-input"
                aria-label="Pause until"
                value={customValue}
                min={toLocalInput(new Date())}
                onChange={(e) => {
                  setCustomValue(e.target.value);
                  setCustomError(null);
                }}
              />
              {customError && (
                <p className="cm-field-error" role="alert">
                  {customError}
                </p>
              )}
              <button type="button" className="cm-btn cm-btn-primary cm-btn-sm" onClick={submitCustom}>
                Pause until then
              </button>
            </div>
          ) : (
            <button type="button" role="menuitem" className="cm-pause-menu-item" onClick={() => setCustom(true)}>
              Until…
            </button>
          )}
        </div>
      )}
    </span>
  );
}
