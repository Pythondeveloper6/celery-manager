import { useCallback, useEffect, useState } from "react";
import { API_BASE } from "../api/base";
import { useAuthOptions } from "../lib/auth";
import { useCan } from "../lib/role";
import { errorMessage } from "../api/tasks";
import { HostHealthPanel } from "../components/HostHealthPanel";

type Annotation = { id: number; kind: string; label: string; revision: string | null; starts_at: string; ends_at: string | null; actor: string; note: string | null };
export function Operations() {
  const canAdmin = useCan("admin"), options = useAuthOptions();
  const [items, setItems] = useState<Annotation[]>([]), [error, setError] = useState<string | null>(null);
  const [kind, setKind] = useState("deployment"), [label, setLabel] = useState(""), [revision, setRevision] = useState("");
  const [end, setEnd] = useState(""), [note, setNote] = useState(""), [busy, setBusy] = useState(false);
  const [checks, setChecks] = useState<{ name: string; state: string; detail: string }[] | null>(null);
  const load = useCallback(async () => {
    try {
      const response = await fetch(`${API_BASE}/operations/annotations`, { credentials: "include" });
      if (!response.ok) throw new Error(await errorMessage(response, "Couldn't load operation annotations."));
      setItems((await response.json()).items); setError(null);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Couldn't load annotations."); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  return <div className="cm-page"><header className="cm-page-header"><h1 className="cm-page-title">Operations</h1><p>Environment: {options.environment || "not labelled"}. Annotations provide context; they never pause jobs or silence alerts.</p></header>
    <HostHealthPanel />
    <section className="cm-card cm-card-pad cm-stack"><h2>Deployment and maintenance history</h2>
      {canAdmin && <form className="cm-stack" onSubmit={async event => {
        event.preventDefault(); setBusy(true); setError(null);
        try {
          const response = await fetch(`${API_BASE}/operations/annotations`, { method: "POST", credentials: "include", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ kind, label, revision: revision || null, note: note || null, ends_at: kind === "maintenance" ? new Date(end).toISOString() : null }) });
          if (!response.ok) throw new Error(await errorMessage(response, "Couldn't add annotation."));
          setLabel(""); setNote(""); await load();
        } catch (cause) { setError(cause instanceof Error ? cause.message : "Couldn't save annotation."); }
        finally { setBusy(false); }
      }}>
        <label>Kind <select className="cm-input" value={kind} disabled={busy} onChange={e => setKind(e.target.value)}><option value="deployment">Deployment</option><option value="maintenance">Planned maintenance</option></select></label>
        <label>Label <input className="cm-input" required maxLength={255} value={label} disabled={busy} onChange={e => setLabel(e.target.value)} /></label>
        <label>Revision <input className="cm-input" maxLength={255} value={revision} disabled={busy} onChange={e => setRevision(e.target.value)} /></label>
        {kind === "maintenance" && <label>Maintenance ends (your local time) <input className="cm-input" type="datetime-local" required value={end} disabled={busy} onChange={e => setEnd(e.target.value)} /></label>}
        <label>Note <textarea className="cm-input" maxLength={2000} value={note} disabled={busy} onChange={e => setNote(e.target.value)} /></label>
        <button className="cm-btn cm-btn-outline" disabled={busy}>Record annotation</button>
      </form>}
      {error && <p className="cm-error" role="alert">{error}</p>}
      {!items.length && <p>No operational annotations recorded.</p>}
      {items.map(item => <article key={item.id}><strong>{item.label}</strong><p>{item.kind} · {new Date(item.starts_at).toLocaleString()} · {item.actor}{item.revision && ` · ${item.revision}`}</p>{item.ends_at && <p>Until {new Date(item.ends_at).toLocaleString()}</p>}{item.note && <p>{item.note}</p>}</article>)}
    </section>
    {canAdmin && <section className="cm-card cm-card-pad"><h2>Read-only preflight</h2><p>Reads storage and configuration. Broker and worker probes are available through the doctor CLI.</p><button className="cm-btn cm-btn-outline" disabled={busy} onClick={async () => { setBusy(true); try { const response = await fetch(`${API_BASE}/operations/preflight`, { credentials: "include" }); if (!response.ok) throw new Error(await errorMessage(response, "Preflight unavailable.")); setChecks((await response.json()).checks); } catch (cause) { setError(cause instanceof Error ? cause.message : "Preflight unavailable."); } finally { setBusy(false); } }}>Run preflight</button>{checks && <ul>{checks.map(check => <li key={check.name}>{check.name}: {check.state} — {check.detail}</li>)}</ul>}</section>}
  </div>;
}
