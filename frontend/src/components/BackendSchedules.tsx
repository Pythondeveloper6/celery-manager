import { useEffect, useState } from "react";
import { API_BASE } from "../api/base";
import { errorMessage } from "../api/tasks";
import { useCan } from "../lib/role";
import type { ScheduleSummary } from "../api/schedules";

async function call(path: string, method: string, body?: unknown) {
  const response = await fetch(`${API_BASE}/schedules${path}`, {
    method, credentials: "include", headers: { "Content-Type": "application/json" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (!response.ok) throw new Error(await errorMessage(response, "The schedule could not be changed."));
  return response.json();
}

export function BackendSchedules({ onChanged }: { onChanged: () => void }) {
  const isAdmin = useCan("admin");
  const [backend, setBackend] = useState<{ name: string; writable: boolean } | null>(null);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [task, setTask] = useState("");
  const [text, setText] = useState("weekdays at 06:30");
  const [args, setArgs] = useState("[]");
  const [kwargs, setKwargs] = useState("{}");
  const [preview, setPreview] = useState<{ schedule: unknown; description: string; occurrences: string[]; timezone: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  useEffect(() => {
    let cancelled = false;
    fetch(`${API_BASE}/schedules/backend`, { credentials: "include" }).then(async (r) => {
      if (!r.ok) throw new Error(await errorMessage(r, "Couldn't read the scheduler backend."));
      return r.json();
    }).then((b) => { if (!cancelled) setBackend(b); })
      .catch((e) => { if (!cancelled) setError(e instanceof Error ? e.message : "Couldn't read the scheduler backend."); });
    return () => { cancelled = true; };
  }, []);
  if (!isAdmin) return null;
  return <section className="cm-card cm-card-pad cm-stack" aria-label="Schedule management">
    {backend && <p className="cm-run-hint">Scheduler: {backend.name}{!backend.writable && ". Definitions are managed in your Celery configuration."}</p>}
    {backend?.writable && <button className="cm-btn cm-btn-outline" type="button" onClick={() => setOpen(!open)} aria-expanded={open}>New schedule</button>}
    {error && <p className="cm-error" role="alert">{error}</p>}
    {notice && <p className="cm-notice" role="status">{notice}</p>}
    {open && <form className="cm-stack" onSubmit={async (event) => {
      event.preventDefault(); if (!preview || busy) return;
      setBusy(true); setError(null);
      try {
        const a = JSON.parse(args), kw = JSON.parse(kwargs);
        if (!Array.isArray(a) || !kw || Array.isArray(kw) || typeof kw !== "object") throw new Error("Arguments must be a JSON array and keyword arguments a JSON object.");
        await call("", "POST", { name, task, schedule: preview.schedule, args: a, kwargs: kw });
        setNotice(`Created ${name}.`); setOpen(false); onChanged();
      } catch (e) { setError(e instanceof Error ? e.message : "Couldn't create the schedule."); }
      finally { setBusy(false); }
    }}>
      <div className="cm-field"><label htmlFor="schedule-name">Schedule name</label><input className="cm-input" id="schedule-name" value={name} required maxLength={255} onChange={(e) => setName(e.target.value)} /></div>
      <div className="cm-field"><label htmlFor="schedule-task">Registered task name</label><input className="cm-input" id="schedule-task" value={task} required onChange={(e) => setTask(e.target.value)} /></div>
      <div className="cm-field"><label htmlFor="schedule-text">When to run</label><input className="cm-input" id="schedule-text" value={text} required disabled={busy} onChange={(e) => { setText(e.target.value); setPreview(null); }} /><p className="cm-field-hint">For example: every 15 minutes, weekdays at 06:30, or a five-field cron expression.</p></div>
      <div className="cm-field"><label htmlFor="schedule-args">Arguments (JSON array)</label><textarea className="cm-input" id="schedule-args" value={args} onChange={(e) => setArgs(e.target.value)} /></div>
      <div className="cm-field"><label htmlFor="schedule-kwargs">Keyword arguments (JSON object)</label><textarea className="cm-input" id="schedule-kwargs" value={kwargs} onChange={(e) => setKwargs(e.target.value)} /></div>
      <button type="button" className="cm-btn cm-btn-outline" disabled={busy} onClick={async () => {
        setBusy(true); setError(null); const requested = text;
        try { const result = await call("/preview", "POST", { type: "text", text: requested }); setPreview(result); }
        catch (e) { setError(e instanceof Error ? e.message : "Couldn't preview the schedule."); }
        finally { setBusy(false); }
      }}>Preview next runs</button>
      {preview && <div role="status"><p>{preview.description} · {preview.timezone}</p><ol>{preview.occurrences.map((t) => <li key={t}>{new Date(t).toLocaleString(undefined, { timeZone: preview.timezone, timeZoneName: "short" })}</li>)}</ol></div>}
      <button type="submit" className="cm-btn cm-btn-primary" disabled={busy || !preview}>{busy ? "Working…" : "Create schedule"}</button>
    </form>}
  </section>;
}

export function BackendScheduleActions({ schedule, onChanged, onError }: { schedule: ScheduleSummary; onChanged: () => void; onError: (message: string) => void }) {
  const isAdmin = useCan("admin");
  const isOperator = useCan("operator");
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const perform = async (action: string, method = "POST") => {
    setBusy(true);
    try {
      const result = await call(`/${encodeURIComponent(schedule.name)}${action}`, method);
      if (result.task_id) setNotice(`Started run ${result.task_id}.`);
      else if (result.target_time) setNotice(`Skipping ${new Date(result.target_time).toLocaleString()}.`);
      else setNotice("Schedule deleted; history was kept.");
      onChanged();
    } catch (e) { onError(e instanceof Error ? e.message : "The action failed."); }
    finally { setBusy(false); setConfirm(false); }
  };
  if (!isOperator) return null;
  return <>
    {schedule.enabled && <button className="cm-btn cm-btn-ghost cm-btn-sm" disabled={busy} onClick={() => perform("/skip-next")}>Skip next run</button>}
    {schedule.enabled && <button className="cm-btn cm-btn-ghost cm-btn-sm" disabled={busy} onClick={() => perform("/run-now")}>Run now</button>}
    {schedule.enabled && schedule.backend_writable && isAdmin && <button className="cm-btn cm-btn-ghost cm-btn-sm" disabled={busy} onClick={() => setConfirm(true)}>Delete schedule</button>}
    {confirm && <div role="group" aria-label={`Confirm deletion of ${schedule.name}`}><p>Delete this schedule? Its run and override history will remain.</p><button className="cm-btn cm-btn-destructive cm-btn-sm" disabled={busy} onClick={() => perform("", "DELETE")}>Delete</button><button className="cm-btn cm-btn-ghost cm-btn-sm" disabled={busy} onClick={() => setConfirm(false)}>Keep schedule</button></div>}
    {notice && <p className="cm-run-hint" role="status">{notice}</p>}
  </>;
}
