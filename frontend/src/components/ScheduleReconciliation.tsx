import { useState } from "react";
import { API_BASE } from "../api/base";
import { errorMessage } from "../api/tasks";

type Change = { name: string; action: string; apply: boolean; before: unknown; after: unknown };
type Plan = { version: number; backend: string; ownership: string; observed_fingerprint: string; changes: Change[] };

async function request(path: string, body?: unknown) {
  const response = await fetch(`${API_BASE}/schedule-reconciliation/${path}`, {
    credentials: "include", method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  if (!response.ok) throw new Error(await errorMessage(response, "Couldn't reconcile schedules."));
  return response.json();
}

export function ScheduleReconciliation({ writable, onChanged }: { writable: boolean; onChanged: () => void }) {
  const [ownership, setOwnership] = useState("backend");
  const [updates, setUpdates] = useState(false);
  const [document, setDocument] = useState("");
  const [plan, setPlan] = useState<Plan | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const invalidate = () => { setPlan(null); setConfirmed(false); setNotice(null); };
  const run = async (operation: () => Promise<void>) => {
    setBusy(true); setError(null);
    try { await operation(); } catch (e) { setError(e instanceof Error ? e.message : "Couldn't reconcile schedules."); }
    finally { setBusy(false); }
  };
  const count = plan?.changes.filter((change) => change.apply).length ?? 0;
  return <details className="cm-stack"><summary>Schedule backup and reconciliation</summary>
    <p className="cm-field-hint">Export the current backend before adoption or repair. Plans compare the desired definitions with the backend; they never delete extra entries. Writes across external schedulers can partially succeed: inspect the backend and generate a new plan after any error.</p>
    <button className="cm-btn cm-btn-outline" disabled={busy} onClick={() => run(async () => {
      const value = await request("export");
      const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }));
      const link = window.document.createElement("a"); link.href = url; link.download = "celery-manager-schedules.json"; link.click(); URL.revokeObjectURL(url);
      setNotice("Downloaded the current backend definitions.");
    })}>Export backend JSON</button>
    <div className="cm-field"><label htmlFor="reconcile-document">Desired export JSON (optional)</label><textarea className="cm-input" id="reconcile-document" rows={5} value={document} disabled={busy} onChange={(event) => { setDocument(event.target.value); invalidate(); }} /><p className="cm-field-hint">Paste a version-1 export to restore or adopt it. Blank compares entries from the current Celery app configuration.</p></div>
    <div className="cm-field"><label htmlFor="reconcile-ownership">Ownership</label><select className="cm-select" id="reconcile-ownership" value={ownership} disabled={busy} onChange={(event) => { setOwnership(event.target.value); invalidate(); }}><option value="backend">Backend: inspect drift only</option><option value="code">Code: adopt desired definitions</option><option value="sql">SQL: adopt into sqlalchemy-beat</option></select></div>
    <label><input type="checkbox" checked={updates} disabled={busy || ownership === "backend"} onChange={(event) => { setUpdates(event.target.checked); invalidate(); }} /> Include repairs of differing existing entries</label>
    <button className="cm-btn cm-btn-outline" disabled={busy} onClick={() => run(async () => {
      invalidate(); setPlan(await request("plan", { ownership, allow_updates: updates, ...(document.trim() ? { document: JSON.parse(document) } : {}) }));
    })}>Generate review plan</button>
    {plan && <section aria-label="Reconciliation plan" className="cm-stack"><p>Backend: {plan.backend}. {plan.changes.length} differences; {count} selected writes.</p>
      {plan.changes.map((change, index) => <details key={change.name}><summary>{change.action}: {change.name}{change.apply ? " · will apply" : " · observation only"}</summary>
        {change.apply && <label><input type="checkbox" checked={change.apply} disabled={busy} onChange={() => { setPlan({ ...plan, changes: plan.changes.map((item, i) => i === index ? { ...item, apply: false } : item) }); setConfirmed(false); }} /> Include this write (regenerate the plan to reselect)</label>}
        <p>Before</p><pre style={{ overflowX: "auto" }}>{JSON.stringify(change.before, null, 2)}</pre><p>After</p><pre style={{ overflowX: "auto" }}>{JSON.stringify(change.after, null, 2)}</pre>
      </details>)}
      {count > 0 && writable && <><label><input type="checkbox" checked={confirmed} disabled={busy} onChange={(event) => setConfirmed(event.target.checked)} /> I reviewed the before/after definitions and approve these {count} writes.</label><button className="cm-btn cm-btn-primary" disabled={busy || !confirmed} onClick={() => run(async () => {
        // Consume approval and plan even on failure: a retry must be freshly reviewed.
        const reviewed = plan; setConfirmed(false); setPlan(null);
        const result: { applied: string[] } = await request("apply", reviewed);
        setNotice(`Applied ${result.applied.length} schedule changes.`); onChanged();
      })}>Apply reviewed changes</button></>}
      {!writable && <p className="cm-field-hint">This backend is read-only; plans cannot be applied.</p>}
    </section>}
    {error && <p className="cm-error" role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
  </details>;
}
