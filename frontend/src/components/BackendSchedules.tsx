import { useEffect, useId, useState } from "react";
import { API_BASE } from "../api/base";
import { errorMessage } from "../api/tasks";
import { useCan } from "../lib/role";
import { ScheduleReconciliation } from "./ScheduleReconciliation";
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
  const [options, setOptions] = useState("{}");
  const [timezone, setTimezone] = useState("");
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
        const { args: a, kwargs: kw, options: opts } = parseInvocation(args, kwargs, options);
        if (!Array.isArray(a) || !kw || Array.isArray(kw) || typeof kw !== "object") throw new Error("Arguments must be a JSON array and keyword arguments a JSON object.");
        await call("", "POST", { name, task, schedule: preview.schedule, args: a, kwargs: kw, options: opts });
        setNotice(`Created ${name}.`); setOpen(false); onChanged();
      } catch (e) { setError(e instanceof Error ? e.message : "Couldn't create the schedule."); }
      finally { setBusy(false); }
    }}>
      <div className="cm-field"><label htmlFor="schedule-name">Schedule name</label><input className="cm-input" id="schedule-name" value={name} required maxLength={255} onChange={(e) => setName(e.target.value)} /></div>
      <div className="cm-field"><label htmlFor="schedule-task">Registered task name</label><input className="cm-input" id="schedule-task" value={task} required onChange={(e) => setTask(e.target.value)} /></div>
      <div className="cm-field"><label htmlFor="schedule-text">When to run</label><input className="cm-input" id="schedule-text" value={text} required disabled={busy} onChange={(e) => { setText(e.target.value); setPreview(null); }} /><p className="cm-field-hint">For example: every 15 minutes, weekdays at 06:30, or a five-field cron expression.</p></div>
      <div className="cm-field"><label htmlFor="schedule-timezone">Timezone (IANA, calendar schedules)</label><input className="cm-input" id="schedule-timezone" placeholder="App timezone" value={timezone} disabled={busy} onChange={(e) => { setTimezone(e.target.value); setPreview(null); }} /><p className="cm-field-hint">For example Europe/London. Leave blank for intervals or the app timezone.</p></div>
      <div className="cm-field"><label htmlFor="schedule-args">Arguments (JSON array)</label><textarea className="cm-input" id="schedule-args" value={args} onChange={(e) => setArgs(e.target.value)} /></div>
      <div className="cm-field"><label htmlFor="schedule-kwargs">Keyword arguments (JSON object)</label><textarea className="cm-input" id="schedule-kwargs" value={kwargs} onChange={(e) => setKwargs(e.target.value)} /></div>
      <div className="cm-field"><label htmlFor="schedule-options">Delivery options (JSON object)</label><textarea className="cm-input" id="schedule-options" value={options} onChange={(e) => setOptions(e.target.value)} /><p className="cm-field-hint">For example {'{"queue":"reports","priority":5}'}. The server validates supported options and reserved headers.</p></div>
      <button type="button" className="cm-btn cm-btn-outline" disabled={busy} onClick={async () => {
        setBusy(true); setError(null); const requested = text;
        try { const result = await call("/preview", "POST", { type: "text", text: requested, ...(timezone.trim() ? { timezone: timezone.trim() } : {}) }); setPreview(result); }
        catch (e) { setError(e instanceof Error ? e.message : "Couldn't preview the schedule."); }
        finally { setBusy(false); }
      }}>Preview next runs</button>
      {preview && <div role="status"><p>{preview.description} · {preview.timezone}</p><ol>{preview.occurrences.map((t) => <li key={t}>{new Date(t).toLocaleString(undefined, { timeZone: preview.timezone, timeZoneName: "short" })}</li>)}</ol></div>}
      <button type="submit" className="cm-btn cm-btn-primary" disabled={busy || !preview}>{busy ? "Working…" : "Create schedule"}</button>
    </form>}
    {backend && <ScheduleReconciliation writable={backend.writable} onChanged={onChanged} />}
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
    {schedule.backend_writable && schedule.definition && <ScheduleInvocationEditor schedule={schedule} onChanged={onChanged} />}
    {notice && <p className="cm-run-hint" role="status">{notice}</p>}
  </>;
}


function parseInvocation(args: string, kwargs: string, options: string) {
  const a = JSON.parse(args), kw = JSON.parse(kwargs), opts = JSON.parse(options);
  if (!Array.isArray(a) || !kw || Array.isArray(kw) || typeof kw !== "object" || !opts || Array.isArray(opts) || typeof opts !== "object") throw new Error("Arguments must be a JSON array; keyword arguments and delivery options must be JSON objects.");
  return { args: a, kwargs: kw, options: opts };
}

function ScheduleInvocationEditor({ schedule, onChanged }: { schedule: ScheduleSummary; onChanged: () => void }) {
  const uid = useId();
  const fingerprint = JSON.stringify({ args: schedule.definition?.args, kwargs: schedule.definition?.kwargs, options: schedule.definition?.options });
  const [baseline, setBaseline] = useState(fingerprint);
  const stale = baseline !== fingerprint;
  const [args, setArgs] = useState(JSON.stringify(schedule.definition?.args ?? [], null, 2));
  const [kwargs, setKwargs] = useState(JSON.stringify(schedule.definition?.kwargs ?? {}, null, 2));
  const [options, setOptions] = useState(JSON.stringify(schedule.definition?.options ?? {}, null, 2));
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  return <details><summary>Edit arguments and delivery options</summary><form className="cm-stack" onSubmit={async (event) => {
    event.preventDefault(); if (busy || stale) return; setBusy(true); setError(null); setNotice(null);
    try { const saved = await call(`/${encodeURIComponent(schedule.name)}`, "PATCH", parseInvocation(args, kwargs, options)); setBaseline(JSON.stringify({ args: saved.args, kwargs: saved.kwargs, options: saved.options })); setNotice("Saved arguments and delivery options."); onChanged(); }
    catch (e) { setError(e instanceof Error ? e.message : "Couldn't save arguments."); }
    finally { setBusy(false); }
  }}>
    {stale && <div role="alert"><p>The backend definition changed. Reload its current arguments and options before saving; this discards your unsaved changes.</p><button type="button" className="cm-btn cm-btn-outline" disabled={busy} onClick={() => { setArgs(JSON.stringify(schedule.definition?.args ?? [], null, 2)); setKwargs(JSON.stringify(schedule.definition?.kwargs ?? {}, null, 2)); setOptions(JSON.stringify(schedule.definition?.options ?? {}, null, 2)); setBaseline(fingerprint); setError(null); setNotice(null); }}>Reload current definition</button></div>}
    {[{ key: "args", label: "Arguments (JSON array)", value: args, set: setArgs }, { key: "kwargs", label: "Keyword arguments (JSON object)", value: kwargs, set: setKwargs }, { key: "options", label: "Delivery options (JSON object)", value: options, set: setOptions }].map((field) => <div className="cm-field" key={field.key}><label htmlFor={`${uid}-${field.key}`}>{field.label}</label><textarea className="cm-input" id={`${uid}-${field.key}`} value={field.value} disabled={busy} onChange={(event) => field.set(event.target.value)} /></div>)}
    <p className="cm-field-hint">Saving replaces these three fields. Existing runs keep their original arguments. Reserved tracking headers cannot be changed.</p>
    <button className="cm-btn cm-btn-outline" disabled={busy || stale}>Save arguments and options</button>
    {notice && <p role="status">{notice}</p>}{error && <p className="cm-error" role="alert">{error}</p>}
  </form></details>;
}
