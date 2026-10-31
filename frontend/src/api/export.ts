import { API_BASE as BASE } from "./base";
import type { TaskQuery } from "./tasks";

export interface ExportResult {
  rows: number;
  total: number;
  truncated: boolean;
}

/** Download the runs matching `filters` as a file. Uses fetch + a blob (not a
 *  bare link) so a failure can be reported and the truncation header read. */
export async function downloadTaskExport(format: "csv" | "json", filters: TaskQuery): Promise<ExportResult> {
  const query = new URLSearchParams({ format });
  for (const key of ["status", "task_name", "q", "date_from", "date_to", "initiator", "started_by_me"] as const) {
    const value = filters[key];
    if (value !== undefined && value !== "") query.set(key, String(value));
  }
  const response = await fetch(`${BASE}/tasks/export?${query}`, { credentials: "include" });
  if (!response.ok) throw new Error("Couldn't export these runs. Check your connection and try again.");

  const blob = await response.blob();
  const match = /filename="([^"]+)"/.exec(response.headers.get("content-disposition") ?? "");
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = match?.[1] ?? `task-runs.${format}`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);

  return {
    rows: Number(response.headers.get("x-export-rows") ?? 0),
    total: Number(response.headers.get("x-export-total") ?? 0),
    truncated: response.headers.get("x-export-truncated") === "true",
  };
}
