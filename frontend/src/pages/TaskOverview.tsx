import "../styles/insights.css";

import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { getOverview, getTaskStats, type Overview, type RangeKey, type TaskStats } from "../api/analytics";
import { FailureGroups } from "../components/FailureGroups";
import { IconChevronLeft } from "../components/icons";
import { Kpi, RangeTabs } from "../components/InsightsParts";
import { StatusBadge } from "../components/Status";
import { RunsChart, RuntimeChart } from "../components/TrendCharts";
import { formatPercent, formatWhen } from "../lib/insights";
import { formatDuration } from "../lib/taskProgress";

export function TaskOverview() {
  const { taskName = "" } = useParams();
  const [range, setRange] = useState<RangeKey>("7d");
  const [stats, setStats] = useState<TaskStats | null>(null);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setError(null);
    Promise.all([getTaskStats(taskName, range), getOverview(range, taskName)])
      .then(([s, o]) => {
        setStats(s);
        setOverview(o);
      })
      .catch((err) => setError(err instanceof Error ? err.message : "Couldn't load this task."));
  }, [taskName, range]);

  useEffect(() => {
    setStats(null);
    setOverview(null);
    load();
  }, [load]);

  const listHref = `/?q=${encodeURIComponent(taskName)}`;

  return (
    <div className="cm-page">
      <Link to="/insights" className="cm-btn cm-btn-ghost cm-btn-sm cm-back-link">
        <IconChevronLeft />
        Insights
      </Link>

      <header className="cm-page-header">
        <div className="cm-page-header-titles">
          <h1 className="cm-page-title">{taskName}</h1>
          {stats && (
            <p className="cm-page-lede">
              Last success {formatWhen(stats.last_success_at)}
              <span aria-hidden="true"> · </span>
              Last failure {formatWhen(stats.last_failure_at)}
            </p>
          )}
        </div>
        <RangeTabs value={range} onChange={setRange} />
      </header>

      {error ? (
        <div className="cm-stack">
          <p className="cm-error" role="alert">
            {error}
          </p>
          <button type="button" className="cm-btn cm-btn-outline" onClick={load}>
            Try again
          </button>
        </div>
      ) : !stats || !overview ? (
        <p className="cm-loading">
          <span className="cm-live-dot" aria-hidden="true" />
          Loading&hellip;
        </p>
      ) : (
        <>
          {stats.truncated && (
            <p className="cm-alert" role="status">
              This period holds more runs than can be analysed at once, so the figures cover the newest runs only.
            </p>
          )}
          <div className="cm-kpi-row cm-kpi-row-6">
            <Kpi label="Runs" value={stats.totals.runs.toLocaleString()} />
            <Kpi label="Success rate" value={formatPercent(stats.totals.success_rate)} />
            <Kpi label="Median runtime" value={formatDuration(stats.runtime.p50)} />
            <Kpi label="p95 runtime" value={formatDuration(stats.runtime.p95)} />
            <Kpi label="Slowest run" value={formatDuration(stats.runtime.max)} />
            <Kpi label="Failures" value={stats.totals.failed.toLocaleString()} tone={stats.totals.failed > 0 ? "danger" : undefined} />
          </div>

          {stats.totals.runs > 0 && (
            <>
              <div className="cm-card cm-card-pad">
                <RunsChart data={overview} />
              </div>
              <div className="cm-card cm-card-pad">
                <RuntimeChart data={overview} />
              </div>
            </>
          )}

          <section className="cm-card cm-card-flush" aria-labelledby="cm-recent-title">
            <div className="cm-card-header cm-section-head">
              <h2 id="cm-recent-title" className="cm-card-title">
                Recent runs
              </h2>
              <Link to={listHref} className="cm-link">
                View all runs
              </Link>
            </div>
            <div className="cm-table-scroll">
              <table className="cm-table cm-table-stack">
                <thead>
                  <tr>
                    <th scope="col">Run</th>
                    <th scope="col">Status</th>
                    <th scope="col">Runtime</th>
                    <th scope="col">Started</th>
                  </tr>
                </thead>
                <tbody>
                  {stats.recent_runs.map((r) => (
                    <tr key={r.task_id}>
                      <td className="cm-cell-primary">
                        <Link to={`/tasks/${r.task_id}`} className="cm-link" title={r.task_id}>
                          {r.task_id.slice(0, 8)}
                        </Link>
                      </td>
                      <td className="cm-col-fit">
                        <StatusBadge status={r.status} />
                      </td>
                      <td>{formatDuration(r.runtime_seconds)}</td>
                      <td className="cm-timestamp">{formatWhen(r.started_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          <section aria-labelledby="cm-errors-title" className="cm-stack">
            <h2 id="cm-errors-title" className="cm-card-title">
              Top errors
            </h2>
            <FailureGroups range={range} taskName={taskName} groups={stats.top_errors} />
          </section>
        </>
      )}
    </div>
  );
}
