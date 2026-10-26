import "../styles/insights.css";

import { useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { getOverview, RANGE_KEYS, type Overview, type RangeKey, type TaskRollup } from "../api/analytics";
import { FailureGroups } from "../components/FailureGroups";
import { Kpi, RangeTabs } from "../components/InsightsParts";
import { RunsChart, RuntimeChart } from "../components/TrendCharts";
import { formatPercent, RANGE_LABEL } from "../lib/insights";
import { formatDuration } from "../lib/taskProgress";

type Tab = "trends" | "failures";

function TopTasks({ title, rows, metric }: { title: string; rows: TaskRollup[]; metric: (t: TaskRollup) => string }) {
  return (
    <section className="cm-card cm-card-pad cm-top-tasks" aria-label={title}>
      <h2 className="cm-card-title">{title}</h2>
      {rows.length === 0 ? (
        <p className="cm-chart-summary">Nothing to show for this period.</p>
      ) : (
        <ol className="cm-top-list">
          {rows.map((t) => (
            <li key={t.task_name}>
              <Link to={`/task-types/${encodeURIComponent(t.task_name)}`} className="cm-link cm-top-name">
                {t.task_name}
              </Link>
              <span className="cm-top-metric">{metric(t)}</span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

export function Insights() {
  const [params, setParams] = useSearchParams();
  const rangeParam = params.get("range");
  const range: RangeKey = RANGE_KEYS.includes(rangeParam as RangeKey) ? (rangeParam as RangeKey) : "24h";
  const tab: Tab = params.get("tab") === "failures" ? "failures" : "trends";

  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState<string | null>(null);

  const update = (next: { range?: RangeKey; tab?: Tab }) => {
    const p = new URLSearchParams(params);
    if (next.range) p.set("range", next.range);
    if (next.tab) p.set("tab", next.tab);
    setParams(p, { replace: true });
  };

  const load = useCallback(() => {
    setError(null);
    getOverview(range)
      .then(setData)
      .catch((err) => setError(err instanceof Error ? err.message : "Couldn't load the trends."));
  }, [range]);

  useEffect(() => {
    setData(null);
    load();
  }, [load]);

  const t = data?.totals;

  return (
    <div className="cm-page">
      <header className="cm-page-header">
        <div className="cm-page-header-titles">
          <h1 className="cm-page-title">Insights</h1>
          <p className="cm-page-lede">How your tasks are doing: volume, reliability, speed and what keeps failing.</p>
        </div>
        <RangeTabs value={range} onChange={(r) => update({ range: r })} />
      </header>

      <div className="cm-tabs-list" role="group" aria-label="Section">
        <button type="button" className="cm-tabs-trigger" aria-pressed={tab === "trends"} onClick={() => update({ tab: "trends" })}>
          Trends
        </button>
        <button type="button" className="cm-tabs-trigger" aria-pressed={tab === "failures"} onClick={() => update({ tab: "failures" })}>
          Failures
        </button>
      </div>

      {tab === "failures" ? (
        <FailureGroups range={range} />
      ) : error ? (
        <div className="cm-stack">
          <p className="cm-error" role="alert">
            {error}
          </p>
          <button type="button" className="cm-btn cm-btn-outline" onClick={load}>
            Try again
          </button>
        </div>
      ) : !data || !t ? (
        <p className="cm-loading">
          <span className="cm-live-dot" aria-hidden="true" />
          Loading trends&hellip;
        </p>
      ) : t.runs === 0 ? (
        <p className="cm-empty-state">No runs in the {RANGE_LABEL[range].toLowerCase()}. Try a longer range, or start a task from the Tasks page.</p>
      ) : (
        <>
          {data.truncated && (
            <p className="cm-alert" role="status">
              This period holds more runs than can be analysed at once, so the figures cover the newest runs only.
            </p>
          )}
          <div className="cm-kpi-row">
            <Kpi label="Runs" value={t.runs.toLocaleString()} sub={`${t.throughput_per_hour}/hour`} />
            <Kpi label="Success rate" value={formatPercent(t.success_rate)} sub="Succeeded of finished" />
            <Kpi label="p95 runtime" value={formatDuration(t.runtime_p95)} sub={`Median ${formatDuration(t.runtime_p50)}`} />
            <Kpi label="Failures" value={t.failed.toLocaleString()} tone={t.failed > 0 ? "danger" : undefined} sub={t.failed > 0 ? "See the Failures tab" : "None"} />
          </div>

          <div className="cm-card cm-card-pad">
            <RunsChart data={data} />
          </div>
          <div className="cm-card cm-card-pad">
            <RuntimeChart data={data} />
          </div>

          <div className="cm-top-grid">
            <TopTasks title="Busiest tasks" rows={data.top_by_volume} metric={(r) => `${r.runs.toLocaleString()} runs`} />
            <TopTasks
              title="Highest failure rate"
              rows={data.top_by_failure_rate}
              metric={(r) => `${formatPercent(r.failure_rate)} · ${r.failed}/${r.runs}`}
            />
          </div>
        </>
      )}
    </div>
  );
}
