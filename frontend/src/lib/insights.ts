import type { RangeKey } from "../api/analytics";

export const RANGE_LABEL: Record<RangeKey, string> = {
  "1h": "Last hour",
  "24h": "Last 24 hours",
  "7d": "Last 7 days",
  "30d": "Last 30 days",
};

export function formatPercent(rate: number | null): string {
  if (rate === null) return "—";
  const pct = rate * 100;
  return `${pct >= 99.95 || pct === 0 ? pct.toFixed(0) : pct.toFixed(1)}%`;
}

export function formatWhen(value: string | null): string {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

/** Tick/row label for a bucket start: clock time for short ranges, date otherwise. */
export function formatBucket(value: string, range: RangeKey): string {
  const d = new Date(value);
  if (range === "1h" || range === "24h") return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  if (range === "7d") return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric" });
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/** A round axis maximum at or above `value` (1, 2, 5 x 10^n). */
export function niceMax(value: number): number {
  if (value <= 0) return 1;
  const exp = Math.pow(10, Math.floor(Math.log10(value)));
  const f = value / exp;
  const nice = f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10;
  return nice * exp;
}
