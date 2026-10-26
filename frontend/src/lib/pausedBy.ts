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
