import type { BulkCancelResult } from "../api/tasks";

export function plural(n: number, word: string): string {
  return `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;
}

/** What a bulk cancel did, in one sentence for the task list's notice line. */
export function describeCancel(result: BulkCancelResult): string {
  const parts = [`Asked workers to cancel ${plural(result.revoked, "run")}.`];
  if (result.skipped_finished > 0) parts.push(`${plural(result.skipped_finished, "run")} had already finished.`);
  if (result.unknown > 0) parts.push(`${plural(result.unknown, "run")} weren't found.`);
  // Revoke is a request to workers; the rows only turn "revoked" when a
  // worker's task-revoked event comes back, so say so rather than look broken.
  if (result.revoked > 0) parts.push("Statuses change as workers confirm.");
  return parts.join(" ");
}
