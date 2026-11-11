/** "1,234", "12.3k", "1.2M": compact token counts for tiles and tables. */
export function formatTokens(value: number): string {
  if (!Number.isFinite(value)) return "—";
  const n = Math.abs(value);
  if (n < 10000) return Math.round(value).toLocaleString("en-US");
  if (n < 1e6) return `${(value / 1e3).toFixed(n < 1e5 ? 1 : 0)}k`;
  return `${(value / 1e6).toFixed(n < 1e7 ? 2 : 1)}M`;
}

/** Dollar cost; null means no price is known, shown as "n/a" (never $0). */
export function formatCost(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "n/a";
  if (value === 0) return "$0.00";
  if (value < 0.01) return `$${value.toFixed(4)}`;
  if (value < 100) return `$${value.toFixed(2)}`;
  return `$${Math.round(value).toLocaleString("en-US")}`;
}
