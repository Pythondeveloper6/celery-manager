import type { Workflow, WorkflowNode } from "../api/workflows";
import { statusLabel } from "./status";

/** One row of the rendered tree: either a task or a "group" marker whose
 *  children are the members that share a group id. */
export type TreeItem =
  | { kind: "task"; node: WorkflowNode; children: TreeItem[] }
  | { kind: "group"; groupId: string; children: TreeItem[] };

/** Build the tree exactly as `parent_id` says. A node whose parent isn't in
 *  the data (purged, or cut by the API cap) is shown at the top level rather
 *  than dropped. Children that share a `group_id` are gathered under one
 *  group marker, placed where the group's first member appeared. */
export function buildTree(nodes: WorkflowNode[]): TreeItem[] {
  const ids = new Set(nodes.map((n) => n.task_id));
  const byParent = new Map<string | null, WorkflowNode[]>();
  for (const node of nodes) {
    const key = node.parent_id && ids.has(node.parent_id) && node.parent_id !== node.task_id ? node.parent_id : null;
    const list = byParent.get(key) ?? [];
    list.push(node);
    byParent.set(key, list);
  }

  const seen = new Set<string>();
  const task = (node: WorkflowNode): TreeItem => {
    seen.add(node.task_id);
    return { kind: "task", node, children: build(node.task_id) };
  };
  const build = (parent: string | null): TreeItem[] => {
    const siblings = byParent.get(parent) ?? [];
    const items: TreeItem[] = [];
    const placedGroups = new Set<string>();
    for (const node of siblings) {
      if (seen.has(node.task_id)) continue; // guards against parent cycles
      if (node.group_id) {
        if (placedGroups.has(node.group_id)) continue;
        placedGroups.add(node.group_id);
        const members = siblings.filter((s) => s.group_id === node.group_id && !seen.has(s.task_id));
        if (members.length > 1) {
          items.push({ kind: "group", groupId: node.group_id, children: members.map((m) => task(m)) });
          continue;
        }
      }
      items.push(task(node));
    }
    return items;
  };
  return build(null);
}

/** Task ids on the path from the top of the tree down to `target`, inclusive. */
export function pathTo(items: TreeItem[], target: string): string[] | null {
  for (const item of items) {
    const below = pathTo(item.children, target);
    if (item.kind === "task") {
      if (item.node.task_id === target) return [target];
      if (below) return [item.node.task_id, ...below];
    } else if (below) {
      return below;
    }
  }
  return null;
}

/** "7 tasks: 5 succeeded, 1 failed, 1 started" — most common status first. */
export function summarize(workflow: Pick<Workflow, "nodes">): string {
  const counts = new Map<string, number>();
  for (const node of workflow.nodes) counts.set(node.status, (counts.get(node.status) ?? 0) + 1);
  const parts = [...counts.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([status, n]) => `${n} ${statusLabel(status).toLowerCase()}`);
  const total = workflow.nodes.length;
  return `${total} ${total === 1 ? "task" : "tasks"}: ${parts.join(", ")}`;
}
