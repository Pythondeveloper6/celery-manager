import { useEffect, useState } from "react";
import { listSingletonLocks, releaseSingletonLock, type SingletonLease } from "../api/locks";
import { Modal } from "./Modal";

export function SingletonLocksDialog({ canRelease, onClose }: { canRelease: boolean; onClose: () => void }) {
  const [leases, setLeases] = useState<SingletonLease[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState<SingletonLease | null>(null);

  useEffect(() => {
    let active = true;
    listSingletonLocks().then(rows => { if (active) setLeases(rows); })
      .catch(err => { if (active) setError(err instanceof Error ? err.message : "Couldn't load locks."); });
    return () => { active = false; };
  }, []);

  async function refresh() {
    setBusy(true);
    setError(null);
    setSelected(null);
    try { setLeases(await listSingletonLocks()); }
    catch (err) { setError(err instanceof Error ? err.message : "Couldn't load locks."); }
    finally { setBusy(false); }
  }

  async function release(lease: SingletonLease) {
    setBusy(true);
    setError(null);
    try {
      await releaseSingletonLock(lease);
      setLeases(rows => rows?.filter(row => row.lease_id !== lease.lease_id) ?? []);
      setSelected(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't release lock. Refresh before retrying.");
    } finally { setBusy(false); }
  }

  return <Modal title="Singleton locks" description="A bounded snapshot of active execution leases. Refresh to inspect current ownership." busy={busy} onClose={onClose}>
    <div className="cm-modal-body">
      {error && <p className="cm-field-error" role="alert">{error}</p>}
      {leases === null && !error && <p role="status">Loading locks…</p>}
      {leases?.length === 0 && <p>No active locks in this snapshot.</p>}
      {leases?.map(lease => <div className="cm-field" key={lease.lease_id} style={{ overflowWrap: "anywhere" }}>
        <strong>{lease.key}</strong>
        <p>{lease.task_name}<br />Task {lease.task_id}<br />Acquired {new Date(lease.acquired_at).toLocaleString()} · {Math.max(0, Math.ceil(lease.ttl_ms / 1000))} seconds remaining at inspection</p>
        {canRelease && <button type="button" className="cm-btn cm-btn-danger" disabled={busy} onClick={() => setSelected(lease)}>Release lock…</button>}
      </div>)}
      {selected && <div className="cm-field" role="alert">
        <strong>Release {selected.key}?</strong>
        <p>This does not stop task {selected.task_id}. It can permit duplicate work while that task is still running, and the old worker may report lost ownership after side effects occur. Stop and reconcile the worker first.</p>
        <p>If ownership changed since inspection, the server refuses the release. This action is audited.</p>
        <button type="button" className="cm-btn cm-btn-danger" disabled={busy} onClick={() => void release(selected)}>Release this inspected lease</button>{" "}
        <button type="button" className="cm-btn cm-btn-secondary" disabled={busy} onClick={() => setSelected(null)}>Cancel</button>
      </div>}
      <div className="cm-modal-actions">
        <button type="button" className="cm-btn cm-btn-secondary" disabled={busy} onClick={() => void refresh()}>Refresh</button>
        <button type="button" className="cm-btn cm-btn-secondary" disabled={busy} onClick={onClose}>Close</button>
      </div>
    </div>
  </Modal>;
}
