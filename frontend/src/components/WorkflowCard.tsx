import "../styles/workflows.css";

import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { getWorkflow, type Workflow } from "../api/workflows";
import { formatDuration } from "../lib/taskProgress";
import { buildTree, pathTo, summarize, type TreeItem } from "../lib/workflowTree";
import { IconChevronRight } from "./icons";
import { StatusBadge } from "./Status";

// Above this many tasks, subtrees start collapsed (except the path to the
// current run) so a chord over hundreds of members doesn't bury the page.
const AUTO_COLLAPSE_ABOVE = 30;

function countTasks(items: TreeItem[]): number {
  return items.reduce((n, item) => n + (item.kind === "task" ? 1 : 0) + countTasks(item.children), 0);
}

function itemKey(item: TreeItem): string {
  return item.kind === "task" ? item.node.task_id : `group:${item.groupId}:${item.children.length}`;
}

/** The chain / group / chord a run belongs to, as an indented tree. Renders
 *  nothing for a run that stands alone, so ordinary tasks see no new UI.
 *  `status` is a refetch trigger: the tree should move when this run does. */
export function WorkflowCard({ taskId, status }: { taskId: string; status: string }) {
  const [workflow, setWorkflow] = useState<Workflow | null>(null);

  useEffect(() => {
    let cancelled = false;
    getWorkflow(taskId)
      .then((data) => {
        if (!cancelled) setWorkflow(data);
      })
      .catch(() => {
        // A workflow card is supplementary; the page works without it.
        if (!cancelled) setWorkflow(null);
      });
    return () => {
      cancelled = true;
    };
  }, [taskId, status]);

  if (!workflow || workflow.nodes.length < 2) return null;
  return <WorkflowTree key={workflow.root_id} workflow={workflow} />;
}

function WorkflowTree({ workflow }: { workflow: Workflow }) {
  const tree = useMemo(() => buildTree(workflow.nodes), [workflow.nodes]);
  const total = workflow.nodes.length;
  const large = total > AUTO_COLLAPSE_ABOVE;

  // Collapsed keys. Items with children are keyed by task id (groups by
  // their id), so the set survives the refetches a live run triggers.
  const [collapsed, setCollapsed] = useState<Set<string>>(() => {
    const result = new Set<string>();
    if (!large) return result;
    const open = new Set(pathTo(tree, workflow.current) ?? []);
    const walk = (items: TreeItem[]) => {
      for (const item of items) {
        if (item.children.length > 0 && !(item.kind === "task" && open.has(item.node.task_id))) {
          result.add(itemKey(item));
        }
        walk(item.children);
      }
    };
    walk(tree);
    return result;
  });

  const toggle = (key: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  const allKeys = useMemo(() => {
    const keys: string[] = [];
    const walk = (items: TreeItem[]) => {
      for (const item of items) {
        if (item.children.length > 0) keys.push(itemKey(item));
        walk(item.children);
      }
    };
    walk(tree);
    return keys;
  }, [tree]);

  const render = (items: TreeItem[], depth: number) => (
    <ul className="cm-wf-list" aria-label={depth === 0 ? "Workflow tasks" : undefined}>
      {items.map((item) => {
        const key = itemKey(item);
        const hasChildren = item.children.length > 0;
        const open = !collapsed.has(key);
        const label =
          item.kind === "group" ? `Group of ${countTasks([item])} tasks` : item.node.task_name;
        return (
          <li key={key} className="cm-wf-item">
            <div
              className="cm-wf-row"
              data-kind={item.kind}
              data-current={item.kind === "task" && item.node.task_id === workflow.current ? "true" : undefined}
            >
              {hasChildren ? (
                <button
                  type="button"
                  className="cm-wf-toggle"
                  aria-label={`${open ? "Collapse" : "Expand"} ${label}`}
                  aria-expanded={open}
                  onClick={() => toggle(key)}
                >
                  <IconChevronRight className="cm-wf-chevron" />
                </button>
              ) : (
                <span className="cm-wf-toggle-spacer" aria-hidden="true" />
              )}
              {item.kind === "group" ? (
                <span className="cm-wf-group">
                  <span className="cm-wf-group-badge">Group</span>
                  <span className="cm-wf-meta">{item.children.length} tasks in parallel</span>
                </span>
              ) : (
                <>
                  <StatusBadge status={item.node.status} />
                  {item.node.task_id === workflow.current ? (
                    <span className="cm-wf-name" aria-current="page">
                      {item.node.task_name} <span className="cm-wf-here">this run</span>
                    </span>
                  ) : (
                    <Link to={`/tasks/${item.node.task_id}`} className="cm-wf-name cm-wf-link">
                      {item.node.task_name}
                    </Link>
                  )}
                  <span className="cm-wf-meta">
                    {item.node.runtime_seconds !== null ? formatDuration(item.node.runtime_seconds) : ""}
                  </span>
                </>
              )}
            </div>
            {hasChildren && open && render(item.children, depth + 1)}
          </li>
        );
      })}
    </ul>
  );

  return (
    <section className="cm-card cm-detail-section" aria-labelledby="cm-wf-heading">
      <div className="cm-wf-head">
        <h2 className="cm-card-title" id="cm-wf-heading">
          Workflow
        </h2>
        {allKeys.length > 0 && (
          <div className="cm-wf-actions">
            <button type="button" className="cm-btn cm-btn-ghost cm-btn-sm" onClick={() => setCollapsed(new Set())}>
              Expand all
            </button>
            <button
              type="button"
              className="cm-btn cm-btn-ghost cm-btn-sm"
              onClick={() => setCollapsed(new Set(allKeys))}
            >
              Collapse all
            </button>
          </div>
        )}
      </div>
      <p className="cm-wf-summary">{summarize(workflow)}</p>
      {render(tree, 0)}
      {workflow.truncated && (
        <p className="cm-run-hint">
          This workflow is larger than the {total} tasks shown here. The newest tasks are left out.
        </p>
      )}
    </section>
  );
}
