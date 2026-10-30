import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { API_BASE } from "../api/base";

type Run = { task_id: string; task_name: string; status: string };
export function RelatedRuns({ taskId }: { taskId: string }) {
  const [kind, setKind] = useState("operation"), [items, setItems] = useState<Run[]>([]);
  const [error, setError] = useState(false), [more, setMore] = useState(false);
  useEffect(() => {
    const controller = new AbortController(); setError(false); setItems([]);
    fetch(`${API_BASE}/tasks/${encodeURIComponent(taskId)}/related?kind=${kind}`, { credentials: "include", signal: controller.signal })
      .then(async response => { if (!response.ok) throw new Error("unavailable"); return response.json(); })
      .then(page => { if (!controller.signal.aborted) { setItems(page.items); setMore(page.has_more); } })
      .catch(() => { if (!controller.signal.aborted) setError(true); });
    return () => controller.abort();
  }, [taskId, kind]);
  return <section className="cm-detail-section"><h2>Related attempts</h2><label>Match by <select className="cm-input" value={kind} onChange={event => setKind(event.target.value)}><option value="operation">Business operation key</option><option value="correlation">Request / trace correlation</option></select></label><p>Only the same tenant is included. Shared keys reveal related work; they do not enforce idempotency.</p>{error ? <p className="cm-error">Related attempts could not be loaded.</p> : <ul>{items.map(run => <li key={run.task_id}><Link className="cm-link" to={`/tasks/${encodeURIComponent(run.task_id)}`}>{run.task_name} · {run.task_id}</Link> — {run.status}</li>)}</ul>}{!error && !items.length && <p>No matching recorded key.</p>}{more && <p>Showing the newest 100 matches.</p>}</section>;
}
