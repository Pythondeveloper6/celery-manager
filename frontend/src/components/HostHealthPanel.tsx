import { useCallback, useEffect, useState } from "react";
import { API_BASE } from "../api/base";
import { errorMessage } from "../api/tasks";

type Probe = { name: string; state: string; metrics: Record<string, number>; reason: string | null; observed_at: string | null };
type Command = { name: string; state: string; due_at: string | null; first_run: boolean };
type Scheduler = { instance_id: string; state: string; heartbeat_at: string | null; commands: Command[] };

export function HostHealthPanel() {
  const [probes, setProbes] = useState<Probe[]>([]);
  const [schedulers, setSchedulers] = useState<Scheduler[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [observed, setObserved] = useState<string | null>(null);
  const load = useCallback(async (signal?: AbortSignal) => {
    setBusy(true);
    try {
      const responses = await Promise.all(["probes", "schedulers"].map(path => fetch(`${API_BASE}/health/${path}`, { credentials: "include", signal })));
      for (const response of responses) if (!response.ok) throw new Error(await errorMessage(response, "Couldn't read host health."));
      const [probePage, schedulerPage] = await Promise.all(responses.map(response => response.json()));
      if (signal?.aborted) return;
      setProbes(probePage.items); setSchedulers(schedulerPage.items);
      setObserved(new Date().toLocaleTimeString()); setError(null);
    } catch (cause) {
      if (!signal?.aborted) setError(cause instanceof Error ? cause.message : "Couldn't read host health.");
    } finally { if (!signal?.aborted) setBusy(false); }
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    const interval = window.setInterval(() => { if (!document.hidden) void load(controller.signal); }, 30_000);
    return () => { controller.abort(); window.clearInterval(interval); };
  }, [load]);
  return <section className="cm-card-pad" aria-label="Host health observations">
    <h2>Host health</h2>
    <p className="cm-field-hint">Stored custom measurements and expected scheduler commands. Refreshing reads observations; it never executes host work.</p>
    <button type="button" className="cm-btn cm-btn-outline cm-btn-sm" disabled={busy} onClick={() => void load()}>{busy ? "Reading…" : "Refresh health"}</button>
    {observed && <span className="cm-field-hint"> Last read {observed}</span>}
    {error && <p className="cm-error" role="alert">{error} Previous observations may be stale.</p>}
    {!error && probes.length === 0 && schedulers.length === 0 && <p>No host probes or scheduler expectations registered.</p>}
    {probes.length > 0 && <div className="cm-table-scroll"><table className="cm-table"><caption>Custom probes</caption><thead><tr><th>Probe</th><th>State</th><th>Observed</th><th>Measurements</th></tr></thead><tbody>{probes.map(probe => <tr key={probe.name}><td>{probe.name}</td><td>{probe.state}{probe.reason ? ` (${probe.reason.replaceAll("_", " ")})` : ""}</td><td>{probe.observed_at ? new Date(probe.observed_at).toLocaleString() : "Never observed"}</td><td>{Object.entries(probe.metrics).map(([key, value]) => `${key}: ${value.toLocaleString()}`).join(" · ") || "—"}</td></tr>)}</tbody></table></div>}
    {schedulers.map(instance => <details key={instance.instance_id} className="cm-card-pad"><summary>{instance.instance_id} — {instance.state}</summary><p>Heartbeat: {instance.heartbeat_at ? new Date(instance.heartbeat_at).toLocaleString() : "Not observed"}</p>{instance.commands.length === 0 ? <p>No expected commands.</p> : <ul>{instance.commands.map(command => <li key={command.name}>{command.name}: {command.state} · due {command.due_at ? new Date(command.due_at).toLocaleString() : "unknown"}{command.first_run ? " · first run has not been observed" : ""}</li>)}</ul>}</details>)}
  </section>;
}
