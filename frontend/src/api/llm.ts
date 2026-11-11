import { API_BASE as BASE } from "./base";
import type { RangeKey } from "./analytics";

export interface LlmUsageRow {
  calls: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  tool_calls: number;
  cost_usd: number | null;
  avg_duration_ms: number | null;
}

export interface LlmModelRow extends LlmUsageRow {
  model: string;
}

export interface LlmTaskRow extends LlmUsageRow {
  task_name: string;
}

export interface LlmSeriesPoint {
  at: string;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number | null;
}

export interface LlmUsage {
  range: RangeKey;
  since: string;
  until: string;
  totals: LlmUsageRow & { errors: number; unpriced_calls: number };
  by_model: LlmModelRow[];
  by_task: LlmTaskRow[];
  series: LlmSeriesPoint[];
  truncated: boolean;
}

export interface LlmCall {
  model: string;
  provider: string | null;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  tool_calls: number;
  cost_usd: number | null;
  duration_ms: number | null;
  status: string;
  created_at: string;
}

export interface LlmRun {
  task_id: string;
  calls: LlmCall[];
  totals: { calls: number; input_tokens: number; output_tokens: number; cost_usd: number | null };
}

export async function getLlmUsage(range: RangeKey, taskName?: string): Promise<LlmUsage> {
  const query = new URLSearchParams({ range });
  if (taskName) query.set("task_name", taskName);
  const response = await fetch(`${BASE}/llm/usage?${query.toString()}`, { credentials: "include" });
  if (!response.ok) throw new Error("Couldn't load the LLM usage.");
  return response.json();
}

export async function getLlmRun(taskId: string, signal?: AbortSignal): Promise<LlmRun> {
  const response = await fetch(`${BASE}/llm/runs/${encodeURIComponent(taskId)}`, { credentials: "include", signal });
  if (!response.ok) throw new Error("Couldn't load the LLM calls.");
  return response.json();
}
