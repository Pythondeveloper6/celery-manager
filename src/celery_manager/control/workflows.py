from __future__ import annotations

from celery_manager.db.models import TaskRun

# Upper bound on nodes returned for one workflow. A chord over a huge group
# would otherwise ship thousands of rows to a browser that renders them all.
MAX_WORKFLOW_NODES = 500


def get_workflow(session, task_id: str, limit: int | None = None) -> dict | None:
    """The whole run tree around `task_id`, or None if the run is unknown.

    The tree is every run sharing the starting run's `root_id` (or the run
    itself as root when it has none), plus the root row. Edges are implied by
    each node's `parent_id` and are rendered exactly as Celery recorded them.
    Oldest rows first, so truncation drops the newest.
    """
    limit = MAX_WORKFLOW_NODES if limit is None else limit
    run = session.query(TaskRun).filter_by(task_id=task_id).first()
    if run is None:
        return None
    root_id = run.root_id or run.task_id
    query = session.query(TaskRun).filter((TaskRun.root_id == root_id) | (TaskRun.task_id == root_id))
    rows = query.order_by(TaskRun.id).limit(limit + 1).all()
    truncated = len(rows) > limit
    rows = rows[:limit]
    # Never lose the run the caller asked about, even when truncating.
    if run.task_id not in {r.task_id for r in rows}:
        rows.append(run)
    return {"root_id": root_id, "current": run.task_id, "nodes": rows, "truncated": truncated}
