import { useEffect, useId, useState } from "react";
import { API_BASE } from "../api/base";
import { errorMessage } from "../api/tasks";
import { useCan } from "../lib/role";

type Entry = { state: string; actor: string; note: string | null; created_at: string };

export function FailureReview({ taskId, onChanged }: { taskId: string | null; onChanged: () => void }) {
  const canOperate = useCan("operator");
  const uid = useId();
  const [id, setId] = useState(taskId ?? "");
  const [note, setNote] = useState("");
  const [history, setHistory] = useState<Entry[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  useEffect(() => { if (taskId) { setId(taskId); setHistory(null); setError(null); setNotice(null); } }, [taskId]);
  async function load() {
    const response = await fetch(`${API_BASE}/dead-letters/${encodeURIComponent(id)}/history`, { credentials: "include" });
    if (!response.ok) throw new Error(await errorMessage(response, "Couldn't load failure history."));
    const body = await response.json(); setHistory(body.items);
  }
  async function act(action?: string) {
    setBusy(true); setError(null); setNotice(null);
    try {
      if (action) {
        const response = await fetch(`${API_BASE}/dead-letters/${encodeURIComponent(id)}/${action}`, {
          method: "POST", credentials: "include", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ note: note || null }),
        });
        if (!response.ok) throw new Error(await errorMessage(response, "Couldn't update the failure."));
        setNotice(`Failure ${action === "reopen" ? "reopened" : action === "ignore" ? "ignored" : "acknowledged"}.`);
        onChanged();
      }
      await load();
    } catch (e) { setError(e instanceof Error ? e.message : "Couldn't review this failure."); }
    finally { setBusy(false); }
  }
  return <details className="cm-card cm-card-pad" open={taskId !== null}>
    <summary>Failure acknowledgement and history</summary>
    <div className="cm-stack">
      <div className="cm-field"><label htmlFor={`${uid}-id`}>Task ID</label><input className="cm-input" id={`${uid}-id`} value={id} onChange={(e) => { setId(e.target.value); setHistory(null); }} /></div>
      <button type="button" className="cm-btn cm-btn-outline" disabled={busy || !id.trim()} onClick={() => act()}>Show history</button>
      {canOperate && <>
        <div className="cm-field"><label htmlFor={`${uid}-note`}>Resolution note</label><textarea className="cm-input" id={`${uid}-note`} value={note} maxLength={2000} onChange={(e) => setNote(e.target.value)} /></div>
        <div className="cm-stack"><button type="button" className="cm-btn cm-btn-outline" disabled={busy || !id.trim()} onClick={() => act("acknowledge")}>Acknowledge</button><button type="button" className="cm-btn cm-btn-outline" disabled={busy || !id.trim()} onClick={() => act("ignore")}>Ignore failure</button><button type="button" className="cm-btn cm-btn-ghost" disabled={busy || !id.trim()} onClick={() => act("reopen")}>Reopen</button></div>
      </>}
      {error && <p className="cm-error" role="alert">{error}</p>}
      {notice && <p className="cm-notice" role="status">{notice}</p>}
      {history && <ul>{history.length === 0 ? <li>No resolution history recorded.</li> : history.map((entry, index) => <li key={index}>{entry.state} · {entry.actor} · {new Date(entry.created_at).toLocaleString()}{entry.note && <p>{entry.note}</p>}</li>)}</ul>}
    </div>
  </details>;
}
