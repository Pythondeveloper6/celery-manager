import { useId, useState } from "react";
import type { FormEvent } from "react";
import { changeOwnPassword, MIN_PASSWORD_LENGTH } from "../api/users";
import { Modal } from "./Modal";

/** The signed-in account changing its own password. Any role. Needs the
 *  current password; the server keeps this browser signed in and ends the
 *  account's other sessions. */
export function ChangePasswordDialog({ onClose }: { onClose: () => void }) {
  const uid = useId();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  const mismatch = repeat !== "" && repeat !== next;
  const canSubmit = !busy && current !== "" && next !== "" && repeat === next;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    try {
      await changeOwnPassword(current, next);
      setDone(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't change the password.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title="Change password"
      description="Other browsers where you're signed in will be signed out."
      busy={busy}
      onClose={onClose}
    >
      {done ? (
        <div className="cm-modal-body">
          <p className="cm-notice" role="status">
            Password changed.
          </p>
          <div className="cm-modal-actions">
            <button type="button" className="cm-btn cm-btn-primary" onClick={onClose}>
              Done
            </button>
          </div>
        </div>
      ) : (
        <form className="cm-modal-body" onSubmit={submit} noValidate>
          <div className="cm-field">
            <label htmlFor={`${uid}-current`}>Current password</label>
            <input
              id={`${uid}-current`}
              className="cm-input"
              type="password"
              autoComplete="current-password"
              value={current}
              onChange={(e) => setCurrent(e.target.value)}
              disabled={busy}
            />
          </div>
          <div className="cm-field">
            <label htmlFor={`${uid}-new`}>New password</label>
            <input
              id={`${uid}-new`}
              className="cm-input"
              type="password"
              autoComplete="new-password"
              value={next}
              onChange={(e) => setNext(e.target.value)}
              disabled={busy}
              aria-describedby={`${uid}-hint`}
            />
            <p className="cm-field-hint" id={`${uid}-hint`}>
              At least {MIN_PASSWORD_LENGTH} characters.
            </p>
          </div>
          <div className="cm-field">
            <label htmlFor={`${uid}-repeat`}>Repeat new password</label>
            <input
              id={`${uid}-repeat`}
              className="cm-input"
              type="password"
              autoComplete="new-password"
              value={repeat}
              onChange={(e) => setRepeat(e.target.value)}
              disabled={busy}
              aria-invalid={mismatch || undefined}
            />
            {mismatch && <p className="cm-field-error">The passwords don&rsquo;t match.</p>}
          </div>
          {error && (
            <p className="cm-error" role="alert">
              {error}
            </p>
          )}
          <div className="cm-modal-actions">
            <button type="button" className="cm-btn cm-btn-outline" onClick={onClose} disabled={busy}>
              Cancel
            </button>
            <button type="submit" className="cm-btn cm-btn-primary" disabled={!canSubmit}>
              {busy ? "Changing…" : "Change password"}
            </button>
          </div>
        </form>
      )}
    </Modal>
  );
}
