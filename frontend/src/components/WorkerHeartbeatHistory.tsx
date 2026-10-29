import { useState } from "react";
import { API_BASE } from "../api/base";
import { errorMessage } from "../api/tasks";

type Sample = { observed_at: string; state: string; active: number | null; processed: number | null };
type Page = { items: Sample[]; total: number; offset: number; retention_days: number };
const PAGE = 60;

export function WorkerHeartbeatHistory({ hostname }: { hostname: string }) {
  const [data, setData] = useState<Page | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function load(offset = 0) {
    setBusy(true); setError(null);
    try {
      const response = await fetch(`${API_BASE}/workers/${encodeURIComponent(hostname)}/heartbeats?limit=${PAGE}&offset=${offset}`, { credentials: "include" });
      if (!response.ok) throw new Error(await errorMessage(response, "Couldn't load worker history."));
      setData(await response.json());
    } catch (e) { setError(e instanceof Error ? e.message : "Couldn't load worker history."); }
    finally { setBusy(false); }
  }
  const chronological = [...(data?.items ?? [])].reverse();
  const maximum = Math.max(1, ...chronological.map((s) => s.active ?? 0));

  return <details className="cm-card-pad">
    <summary>Recorded heartbeat history</summary>
    <div className="cm-stack">
      <p className="cm-field-hint">One observation per minute, newest first. Missing samples indicate missing observations; they do not prove the worker stopped.</p>
      <button className="cm-btn cm-btn-outline cm-btn-sm" disabled={busy} type="button" onClick={() => load()}>Load recent history</button>
      {error && <p className="cm-error" role="alert">{error}</p>}
      {data && <>
        {data.retention_days === 0 && <p>History collection is disabled. Last-seen worker state is still available.</p>}
        {data.items.length === 0 ? <p>No heartbeat samples recorded.</p> : <>
          <svg viewBox="0 0 300 60" width="100%" height="100" role="img" aria-label="Active tasks in observed heartbeat samples"><title>Active tasks over the displayed heartbeat samples; maximum {maximum}</title>{chronological.map((sample, index) => sample.active === null ? null : <circle key={sample.observed_at} cx={chronological.length < 2 ? 150 : index * 300 / (chronological.length - 1)} cy={55 - Math.max(0, sample.active) * 50 / maximum} r="2" fill="currentColor" />)}</svg>
          <div className="cm-table-scroll"><table className="cm-table"><thead><tr><th>Observed</th><th>State</th><th>Active</th><th>Processed</th></tr></thead><tbody>{data.items.map((sample) => <tr key={sample.observed_at}><td>{new Date(sample.observed_at).toLocaleString()}</td><td>{sample.state}</td><td>{sample.active ?? "Unknown"}</td><td>{sample.processed ?? "Unknown"}</td></tr>)}</tbody></table></div>
        </>}
        <p>{data.total.toLocaleString()} samples · {data.retention_days || "disabled"} day retention</p>
        <div><button className="cm-btn cm-btn-ghost cm-btn-sm" disabled={busy || data.offset === 0} onClick={() => load(Math.max(0, data.offset - PAGE))}>Newer samples</button><button className="cm-btn cm-btn-ghost cm-btn-sm" disabled={busy || data.offset + PAGE >= data.total} onClick={() => load(data.offset + PAGE)}>Older samples</button></div>
      </>}
    </div>
  </details>;
}
