import { useEffect, useState } from "react";
import { listOwnSessions, revokeOwnSession, type BrowserSession } from "../api/sessions";
import { Modal } from "./Modal";

export function BrowserSessionsDialog({ onClose }: { onClose: () => void }) {
  const [sessions, setSessions] = useState<BrowserSession[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let active = true;
    listOwnSessions().then(rows => { if (active) setSessions(rows); })
      .catch(err => { if (active) setError(err instanceof Error ? err.message : "Couldn't load sessions."); });
    return () => { active = false; };
  }, []);

  async function revoke(session: BrowserSession) {
    if (session.current && !window.confirm("End this browser's dashboard session? You may need to sign in again.")) return;
    setBusy(true);
    setError(null);
    try {
      await revokeOwnSession(session.session_id);
      if (session.current) { window.location.reload(); return; }
      setSessions(rows => rows?.filter(row => row.session_id !== session.session_id) ?? []);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't end this session.");
    } finally {
      setBusy(false);
    }
  }

  return <Modal title="Browser sessions" description="End dashboard sessions you no longer use. Your host application's own login is managed separately." busy={busy} onClose={onClose}>
    <div className="cm-modal-body">
      {error && <p className="cm-field-error" role="alert">{error}</p>}
      {sessions === null && !error && <p role="status">Loading sessions…</p>}
      {sessions?.length === 0 && <p>No active dashboard browser sessions. Host logins and older cookies may be managed separately.</p>}
      {sessions?.map(session => <div className="cm-field" key={session.session_id}>
        <strong>{session.current ? "This browser" : "Another browser session"}</strong>
        <p>Signed in {new Date(session.created_at).toLocaleString()}<br />Expires {new Date(session.expires_at).toLocaleString()}</p>
        <button type="button" className="cm-btn cm-btn-danger" disabled={busy} onClick={() => void revoke(session)}>
          {session.current ? "End this session…" : "End session"}
        </button>
      </div>)}
      <div className="cm-modal-actions"><button type="button" className="cm-btn cm-btn-secondary" disabled={busy} onClick={onClose}>Close</button></div>
    </div>
  </Modal>;
}
