export function formatPausedBy(by: string | null, at: string | null): string {
  const when = at ? new Date(at) : null;
  const time =
    when && !Number.isNaN(when.getTime())
      ? when.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })
      : null;
  if (by && time) return `by ${by}, ${time}`;
  if (by) return `by ${by}`;
  return time ?? "";
}

// "Paused until Tue, Oct 27, 9:00 AM" or "Paused indefinitely", in local time.
export function formatPauseState(until: string | null): string {
  const when = until ? new Date(until) : null;
  if (!when || Number.isNaN(when.getTime())) return "Paused indefinitely";
  return `Paused until ${when.toLocaleString(undefined, { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}`;
}
