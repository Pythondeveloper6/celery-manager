import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { API_BASE } from "../api/base";

type Annotation = { id: number; label: string; revision: string | null };
type Context = { deployment: Annotation | null; maintenance: Annotation[]; has_more: boolean };
export function OperationalContext({ at }: { at: string }) {
  const [context, setContext] = useState<Context | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const controller = new AbortController(); setContext(null); setFailed(false);
    fetch(`${API_BASE}/operations/context?at=${encodeURIComponent(at)}`, { credentials: "include", signal: controller.signal })
      .then(async response => { if (!response.ok) throw new Error("unavailable"); return response.json(); })
      .then(value => { if (!controller.signal.aborted) setContext(value); })
      .catch(() => { if (!controller.signal.aborted) setFailed(true); });
    return () => controller.abort();
  }, [at]);
  if (!failed && !context?.deployment && !context?.maintenance.length) return null;
  return <section className="cm-detail-section"><h2>Deployment and maintenance context</h2>
    <p>At this run's start or dispatch time. Annotations provide context and do not suppress alerts.</p>
    {failed && <p className="cm-error">Operational context could not be loaded.</p>}
    {context?.deployment && <p>Deployment: {context.deployment.label}{context.deployment.revision && ` · ${context.deployment.revision}`}</p>}
    {context?.maintenance.map(item => <p key={item.id}>Planned maintenance: {item.label}</p>)}
    {context?.has_more && <p>Showing the newest 200 active maintenance annotations.</p>}
    <Link className="cm-link" to="/operations">View operations history</Link>
  </section>;
}
