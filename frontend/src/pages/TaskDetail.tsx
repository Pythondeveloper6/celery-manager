import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { getTask, type TaskDetail as TaskDetailData } from "../api/tasks";

function formatPayload(raw: string): string {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

export function TaskDetail() {
  const { taskId } = useParams<{ taskId: string }>();
  const [task, setTask] = useState<TaskDetailData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setTask(null);
    setError(null);
    if (!taskId) return;
    getTask(taskId)
      .then(setTask)
      .catch((err) => setError(err instanceof Error ? err.message : "Task not found."));
  }, [taskId]);

  return (
    <div className="cm-page cm-detail-page">
      <Link to="/" className="cm-back-link">
        &larr; All tasks
      </Link>

      {error ? (
        <p className="cm-error" role="alert">
          {error}
        </p>
      ) : !task ? (
        <p className="cm-loading">
          <span className="cm-live-dot" aria-hidden="true" />
          Loading&hellip;
        </p>
      ) : (
        <>
          <header className="cm-detail-header">
            <h1 className="cm-page-title">{task.task_name}</h1>
            <span className="cm-status-badge" data-status={task.status}>
              {task.status}
            </span>
          </header>
          <p className="cm-task-id cm-detail-task-id">{task.task_id}</p>

          <dl className="cm-detail-meta">
            <div>
              <dt>Args</dt>
              <dd>
                <pre className="cm-code-block">{formatPayload(task.args)}</pre>
              </dd>
            </div>
            <div>
              <dt>Kwargs</dt>
              <dd>
                <pre className="cm-code-block">{formatPayload(task.kwargs)}</pre>
              </dd>
            </div>
          </dl>

          {task.exception && (
            <section className="cm-panel cm-failure-panel">
              <h2>Why it failed</h2>
              <p className="cm-failure-message">{task.exception}</p>
              {task.traceback && <pre className="cm-traceback">{task.traceback}</pre>}
            </section>
          )}

          <section className="cm-panel cm-log-panel">
            <h2>Log output</h2>
            {task.log_output ? (
              <pre className="cm-log-output">{task.log_output}</pre>
            ) : (
              <p className="cm-empty-state">No log output captured for this run.</p>
            )}
          </section>
        </>
      )}
    </div>
  );
}
