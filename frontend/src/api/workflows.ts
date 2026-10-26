import { API_BASE as BASE } from "./base";

export interface WorkflowNode {
  task_id: string;
  task_name: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
  runtime_seconds: number | null;
  parent_id: string | null;
  group_id: string | null;
}

export interface Workflow {
  root_id: string;
  current: string;
  nodes: WorkflowNode[];
  edges: { from: string; to: string }[];
  // True when the run tree had more nodes than the API returns (500).
  truncated: boolean;
}

export async function getWorkflow(taskId: string): Promise<Workflow> {
  const response = await fetch(`${BASE}/tasks/${encodeURIComponent(taskId)}/workflow`, { credentials: "include" });
  if (!response.ok) throw new Error("Couldn't load this workflow.");
  return response.json();
}
