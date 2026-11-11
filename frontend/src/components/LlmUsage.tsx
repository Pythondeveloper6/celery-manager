import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import type { RangeKey } from "../api/analytics";
import { getLlmUsage, type LlmUsage as LlmUsageData, type LlmUsageRow } from "../api/llm";
import { formatCost, formatTokens } from "../lib/llm";
import { formatDuration } from "../lib/taskProgress";
import { Kpi } from "./InsightsParts";
import { LlmCostChart } from "./TrendCharts";

function UsageTable<R extends LlmUsageRow>({ title, label, rows, nameOf, link }: { title: string; label: string; rows: R[]; nameOf: (r: R) => string; link?: boolean }) {
  return (
    <section className="cm-card cm-card-pad" aria-label={title}>
      <h3 className="cm-card-title">{title}</h3>
      <div className="cm-table-scroll">
        <table className="cm-table">
          <thead>
            <tr>
              <th scope="col">{label}</th>
              <th scope="col">Calls</th>
              <th scope="col">Tokens in</th>
              <th scope="col">Tokens out</th>
              <th scope="col">Cost</th>
              <th scope="col">Avg time</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const name = nameOf(r);
              return (
                <tr key={name}>
                  <th scope="row">
                    {link ? (
                      <Link to={`/task-types/${encodeURIComponent(name)}`} className="cm-link">
                        {name}
                      </Link>
                    ) : (
                      name
                    )}
                  </th>
                  <td>{r.calls.toLocaleString()}</td>
                  <td>{formatTokens(r.input_tokens)}</td>
                  <td>{formatTokens(r.output_tokens)}</td>
                  <td>{formatCost(r.cost_usd)}</td>
                  <td>{r.avg_duration_ms === null ? "—" : formatDuration(r.avg_duration_ms / 1000)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

/** LLM spend for the Insights page, following its range selector. */
export function LlmUsageSection({ range }: { range: RangeKey }) {
  const [data, setData] = useState<LlmUsageData | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    setError(null);
    getLlmUsage(range)
      .then(setData)
      .catch((err) => setError(err instanceof Error ? err.message : "Couldn't load the LLM usage."));
  }, [range]);

  useEffect(() => {
    setData(null);
    load();
  }, [load]);

  const t = data?.totals;
  let body;
  if (error) {
    body = (
      <div className="cm-stack">
        <p className="cm-error" role="alert">
          {error}
        </p>
        <button type="button" className="cm-btn cm-btn-outline" onClick={load}>
          Try again
        </button>
      </div>
    );
  } else if (!data || !t) {
    body = (
      <p className="cm-loading">
        <span className="cm-live-dot" aria-hidden="true" />
        Loading LLM usage&hellip;
      </p>
    );
  } else if (t.calls === 0) {
    body = <p className="cm-empty-state">No LLM calls recorded. See docs/llm-usage.md</p>;
  } else {
    body = (
      <>
        {data.truncated && (
          <p className="cm-alert" role="status">
            This period holds more LLM calls than can be analysed at once, so the figures cover the newest calls only.
          </p>
        )}
        {t.unpriced_calls > 0 && (
          <p className="cm-alert" role="status">
            {t.unpriced_calls.toLocaleString()} call{t.unpriced_calls === 1 ? " has" : "s have"} no known price, so the cost covers priced calls only.
          </p>
        )}
        <div className="cm-kpi-row cm-kpi-row-5">
          <Kpi label="LLM calls" value={t.calls.toLocaleString()} sub={`${t.tool_calls.toLocaleString()} tool calls`} />
          <Kpi label="Tokens in" value={formatTokens(t.input_tokens)} sub={`${formatTokens(t.cache_read_tokens)} from cache`} />
          <Kpi label="Tokens out" value={formatTokens(t.output_tokens)} />
          <Kpi label="Cost" value={formatCost(t.cost_usd)} sub={t.unpriced_calls > 0 ? `${t.unpriced_calls.toLocaleString()} unpriced` : undefined} />
          <Kpi label="LLM errors" value={t.errors.toLocaleString()} tone={t.errors > 0 ? "danger" : undefined} />
        </div>
        <div className="cm-card cm-card-pad">
          <LlmCostChart points={data.series} range={range} />
        </div>
        <div className="cm-top-grid">
          <UsageTable title="By model" label="Model" rows={data.by_model} nameOf={(r) => r.model} />
          <UsageTable title="By task" label="Task" rows={data.by_task} nameOf={(r) => r.task_name} link />
        </div>
      </>
    );
  }

  return (
    <section className="cm-stack cm-llm-section" aria-label="LLM usage">
      <h2 className="cm-card-title">LLM usage</h2>
      {body}
    </section>
  );
}
