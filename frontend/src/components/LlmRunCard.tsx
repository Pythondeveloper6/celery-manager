import { useEffect, useState } from "react";
import { getLlmRun, type LlmRun } from "../api/llm";
import { formatCost, formatTokens } from "../lib/llm";
import { formatDuration } from "../lib/taskProgress";

/** The LLM calls a run made. Renders nothing while loading, on error, or when the run made none. */
export function LlmRunCard({ taskId }: { taskId: string }) {
  const [run, setRun] = useState<LlmRun | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setRun(null);
    getLlmRun(taskId, controller.signal)
      .then((r) => {
        if (!controller.signal.aborted) setRun(r);
      })
      .catch(() => {
        // Optional card: a failed lookup just leaves it hidden.
      });
    return () => controller.abort();
  }, [taskId]);

  if (!run || run.calls.length === 0) return null;
  const t = run.totals;

  return (
    <section className="cm-card cm-detail-section" aria-labelledby="cm-llm-heading">
      <h2 className="cm-card-title" id="cm-llm-heading">
        LLM calls
      </h2>
      <p className="cm-run-hint">
        {t.calls.toLocaleString()} call{t.calls === 1 ? "" : "s"} · {formatTokens(t.input_tokens)} tokens in · {formatTokens(t.output_tokens)} out · {formatCost(t.cost_usd)}
      </p>
      <div className="cm-table-scroll">
        <table className="cm-table">
          <thead>
            <tr>
              <th scope="col">Model</th>
              <th scope="col">Tokens in</th>
              <th scope="col">Tokens out</th>
              <th scope="col">Tools</th>
              <th scope="col">Cost</th>
              <th scope="col">Time</th>
              <th scope="col">Status</th>
            </tr>
          </thead>
          <tbody>
            {run.calls.map((c, i) => (
              <tr key={`${c.created_at}-${i}`}>
                <th scope="row">{c.model}</th>
                <td>{formatTokens(c.input_tokens)}</td>
                <td>{formatTokens(c.output_tokens)}</td>
                <td>{c.tool_calls.toLocaleString()}</td>
                <td>{formatCost(c.cost_usd)}</td>
                <td>{c.duration_ms === null ? "—" : formatDuration(c.duration_ms / 1000)}</td>
                <td>{c.status}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
