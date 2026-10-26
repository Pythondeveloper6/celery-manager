import { useEffect, useRef, useState } from "react";
import { pauseSchedule, resumeSchedule, type ScheduleSummary } from "../api/schedules";

const CONFIRM_MS = 5000;

interface Props {
  name: string;
  paused: boolean;
  size?: "sm" | "md";
  // Called with the updated summary after a successful pause or resume.
  onChange: (summary: ScheduleSummary) => void;
  // Called with an error message, or null when a new attempt starts.
  onError: (message: string | null) => void;
}

// Pause / Resume for a whole schedule. Pausing is significant, so the first
// click arms an inline "Confirm pause" / "Keep running" pair that disarms
// itself after a few seconds; resuming is harmless and fires at once.
export function ScheduleToggle({ name, paused, size = "sm", onChange, onError }: Props) {
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const sm = size === "sm" ? " cm-btn-sm" : "";

  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );

  function disarm() {
    if (timer.current) clearTimeout(timer.current);
    setArmed(false);
  }

  function arm() {
    onError(null);
    setArmed(true);
    timer.current = setTimeout(() => setArmed(false), CONFIRM_MS);
  }

  async function run(action: typeof pauseSchedule) {
    disarm();
    onError(null);
    setBusy(true);
    try {
      onChange(await action(name));
    } catch (err) {
      onError(err instanceof Error ? err.message : "Couldn't update this schedule. Try again.");
    } finally {
      setBusy(false);
    }
  }

  if (paused) {
    return (
      <button type="button" className={`cm-btn cm-btn-primary${sm}`} disabled={busy} onClick={() => run(resumeSchedule)}>
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
  if (armed) {
    return (
      <span className="cm-occurrence-actions" role="group" aria-label={`Confirm pausing ${name}`}>
        <button type="button" className={`cm-btn cm-btn-destructive${sm}`} onClick={() => run(pauseSchedule)}>
          Confirm pause
        </button>
        <button type="button" className={`cm-btn cm-btn-ghost${sm}`} onClick={disarm}>
          Keep running
        </button>
      </span>
    );
  }
  return (
    <button type="button" className={`cm-btn cm-btn-outline${sm}`} onClick={arm}>
      Pause schedule
    </button>
  );
}
