import "../styles/users.css";

import { useCallback, useEffect, useId, useState } from "react";
import type { FormEvent } from "react";
import type { Role } from "../api/client";
import {
  createUser,
  deleteUser,
  listUsers,
  MIN_PASSWORD_LENGTH,
  resetUserPassword,
  setUserRole,
  type DashboardUser,
} from "../api/users";
import { Modal } from "../components/Modal";
import { IconCheck, IconCopy } from "../components/icons";
import { useAuthOptions } from "../lib/auth";
import { needsRole, useCan, useRole, useUsername } from "../lib/role";

const ROLES: Role[] = ["viewer", "operator", "admin"];

function RoleSelect({
  id,
  value,
  onChange,
  disabled,
  label,
}: {
  id?: string;
  value: Role;
  onChange: (role: Role) => void;
  disabled?: boolean;
  label?: string;
}) {
  return (
    <select
      id={id}
      className="cm-select cm-users-role"
      value={value}
      disabled={disabled}
      aria-label={label}
      onChange={(e) => onChange(e.target.value as Role)}
    >
      {ROLES.map((role) => (
        <option key={role} value={role}>
          {role}
        </option>
      ))}
    </select>
  );
}

function AddUserDialog({ onClose, onCreated }: { onClose: () => void; onCreated: (user: DashboardUser) => void }) {
  const uid = useId();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<Role>("viewer");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canSubmit = !busy && username.trim() !== "" && password !== "";

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    try {
      onCreated(await createUser(username.trim(), password, role));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't add the user.");
      setBusy(false);
    }
  }

  return (
    <Modal title="Add user" description="They can sign in straight away. The change is recorded in the audit log." busy={busy} onClose={onClose}>
      <form className="cm-modal-body" onSubmit={submit} noValidate>
        <div className="cm-field">
          <label htmlFor={`${uid}-name`}>Username</label>
          <input
            id={`${uid}-name`}
            className="cm-input"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            maxLength={64}
            disabled={busy}
          />
        </div>
        <div className="cm-field">
          <label htmlFor={`${uid}-password`}>Password</label>
          <input
            id={`${uid}-password`}
            className="cm-input"
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            disabled={busy}
            aria-describedby={`${uid}-hint`}
          />
          <p className="cm-field-hint" id={`${uid}-hint`}>
            At least {MIN_PASSWORD_LENGTH} characters. Tell them to change it after signing in.
          </p>
        </div>
        <div className="cm-field">
          <label htmlFor={`${uid}-role`}>Role</label>
          <RoleSelect id={`${uid}-role`} value={role} onChange={setRole} disabled={busy} />
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
            {busy ? "Adding…" : "Add user"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

function ResetPasswordDialog({ username, onClose }: { username: string; onClose: () => void }) {
  const uid = useId();
  const [mode, setMode] = useState<"set" | "generate">("set");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Set once the reset has gone through. A generated password exists only in
  // this response, so the dialog stays open on it until it is dismissed.
  const [result, setResult] = useState<{ generated: string | null } | null>(null);
  const [copied, setCopied] = useState(false);

  const canSubmit = !busy && (mode === "generate" || password !== "");

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!canSubmit) return;
    setBusy(true);
    setError(null);
    try {
      const generated = await resetUserPassword(username, mode === "generate" ? { generate: true } : { password });
      setResult({ generated });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't reset the password.");
    } finally {
      setBusy(false);
    }
  }

  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      // Clipboard blocked (insecure origin, permissions): the password is
      // still selectable on screen.
    }
  }

  return (
    <Modal
      title={`Reset password for ${username}`}
      description="Every session they have open is signed out."
      busy={busy}
      onClose={onClose}
    >
      {result ? (
        <div className="cm-modal-body">
          {result.generated ? (
            <>
              <p className="cm-notice" role="status">
                Password reset. This is the only time it is shown, so pass it on now.
              </p>
              <div className="cm-generated">
                <code>{result.generated}</code>
                <button
                  type="button"
                  className="cm-btn cm-btn-outline cm-btn-icon"
                  aria-label={copied ? "Copied" : "Copy password"}
                  onClick={() => void copy(result.generated as string)}
                >
                  {copied ? <IconCheck /> : <IconCopy />}
                </button>
              </div>
            </>
          ) : (
            <p className="cm-notice" role="status">
              Password reset.
            </p>
          )}
          <div className="cm-modal-actions">
            <button type="button" className="cm-btn cm-btn-primary" onClick={onClose}>
              Done
            </button>
          </div>
        </div>
      ) : (
        <form className="cm-modal-body" onSubmit={submit} noValidate>
          <fieldset className="cm-modal-choice" disabled={busy}>
            <legend className="cm-visually-hidden">New password</legend>
            <label>
              <input type="radio" name={`${uid}-mode`} checked={mode === "set"} onChange={() => setMode("set")} />
              Choose a password
            </label>
            <label>
              <input
                type="radio"
                name={`${uid}-mode`}
                checked={mode === "generate"}
                onChange={() => setMode("generate")}
              />
              Generate a random one
            </label>
          </fieldset>
          {mode === "set" && (
            <div className="cm-field">
              <label htmlFor={`${uid}-password`}>New password</label>
              <input
                id={`${uid}-password`}
                className="cm-input"
                type="password"
                autoComplete="new-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                disabled={busy}
                aria-describedby={`${uid}-hint`}
              />
              <p className="cm-field-hint" id={`${uid}-hint`}>
                At least {MIN_PASSWORD_LENGTH} characters.
              </p>
            </div>
          )}
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
              {busy ? "Resetting…" : "Reset password"}
            </button>
          </div>
        </form>
      )}
    </Modal>
  );
}

export function Users() {
  const isAdmin = useCan("admin");
  const me = useRole();
  const authOptions = useAuthOptions();
  // Marks "you" and hides actions the server would refuse on your own row.
  const currentUsername = useUsername();
  const [users, setUsers] = useState<DashboardUser[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  // Refusals from a row action (the last admin, a deleted account…): shown
  // above the table in the server's words.
  const [actionError, setActionError] = useState<string | null>(null);
  const [busyUser, setBusyUser] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [resetting, setResetting] = useState<string | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState<string | null>(null);

  const refresh = useCallback(() => {
    setLoadError(null);
    listUsers()
      .then(setUsers)
      .catch((err) => setLoadError(err instanceof Error ? err.message : "Couldn't load users."));
  }, []);

  useEffect(() => {
    if (isAdmin) refresh();
  }, [isAdmin, refresh]);

  async function changeRole(user: DashboardUser, role: Role) {
    setBusyUser(user.username);
    setActionError(null);
    try {
      await setUserRole(user.username, role);
      refresh();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Couldn't change the role.");
    } finally {
      setBusyUser(null);
    }
  }

  async function remove(user: DashboardUser) {
    setBusyUser(user.username);
    setActionError(null);
    try {
      await deleteUser(user.username);
      setConfirmingDelete(null);
      refresh();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Couldn't delete the user.");
      setConfirmingDelete(null);
    } finally {
      setBusyUser(null);
    }
  }

  if (!isAdmin) {
    return (
      <div className="cm-page">
        <header className="cm-page-header">
          <h1 className="cm-page-title">Users</h1>
        </header>
        <div className="cm-card cm-card-flush">
          <p className="cm-empty-state">Only an admin can manage users. {needsRole("admin")} (you are {me}).</p>
        </div>
      </div>
    );
  }

  return (
    <div className="cm-page">
      <header className="cm-page-header">
        <div className="cm-page-header-titles">
          <h1 className="cm-page-title">{authOptions.external ? "Local accounts" : "Users"}</h1>
          <p className="cm-page-lede">
            {authOptions.external
              ? `Accounts stored by this dashboard itself. People who sign in with ${authOptions.provider_name} get their role from ${authOptions.provider_name} and aren't listed here.`
              : "Who can sign in to this dashboard, and what they can do."}
          </p>
        </div>
        <div className="cm-users-header-actions">
          <button type="button" className="cm-btn cm-btn-primary" onClick={() => setAdding(true)}>
            Add user
          </button>
        </div>
      </header>

      {!authOptions.local_login && (
        <p className="cm-notice" role="status">
          Signing in with these accounts is turned off (<code>local_login_enabled=False</code>). They are kept, and work
          again if it is turned back on.
        </p>
      )}

      <div className="cm-card cm-card-flush">
        {actionError && (
          <p className="cm-error cm-users-banner" role="alert">
            {actionError}
          </p>
        )}
        {loadError ? (
          <div className="cm-card-pad cm-stack">
            <p className="cm-error" role="alert">
              {loadError}
            </p>
            <button type="button" className="cm-btn cm-btn-outline" onClick={refresh}>
              Try again
            </button>
          </div>
        ) : users === null ? (
          <div className="cm-card-pad">
            <p className="cm-loading">
              <span className="cm-live-dot" aria-hidden="true" />
              Loading users&hellip;
            </p>
          </div>
        ) : users.length === 0 ? (
          <p className="cm-empty-state">No dashboard users yet.</p>
        ) : (
          <div className="cm-table-scroll">
            <table className="cm-table cm-table-stack">
              <thead>
                <tr>
                  <th scope="col">User</th>
                  <th scope="col" className="cm-col-fit">
                    Role
                  </th>
                  <th scope="col" className="cm-col-fit">
                    <span className="cm-visually-hidden">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {users.map((user) => {
                  const isMe = user.username === currentUsername;
                  const local = user.source === "local";
                  const busy = busyUser === user.username;
                  return (
                    <tr key={user.username}>
                      <td className="cm-cell-primary">
                        {user.username}
                        {isMe && (
                          <>
                            {" "}
                            <span className="cm-badge">You</span>
                          </>
                        )}
                      </td>
                      <td className="cm-col-fit">
                        <RoleSelect
                          value={user.role}
                          label={`Role for ${user.username}`}
                          disabled={busy || !local}
                          onChange={(role) => void changeRole(user, role)}
                        />
                      </td>
                      <td className="cm-col-fit">
                        <div className="cm-users-actions">
                          {confirmingDelete === user.username ? (
                            <>
                              <span className="cm-users-confirm">Delete {user.username}?</span>
                              <button
                                type="button"
                                className="cm-btn cm-btn-destructive cm-btn-sm"
                                disabled={busy}
                                onClick={() => void remove(user)}
                              >
                                {busy ? "Deleting…" : "Delete"}
                              </button>
                              <button
                                type="button"
                                className="cm-btn cm-btn-outline cm-btn-sm"
                                disabled={busy}
                                onClick={() => setConfirmingDelete(null)}
                              >
                                Cancel
                              </button>
                            </>
                          ) : (
                            <>
                              {/* Your own password goes through "Change password"
                                  in the sidebar, which asks for the current one. */}
                              {!isMe && local && (
                                <button
                                  type="button"
                                  className="cm-btn cm-btn-outline cm-btn-sm"
                                  onClick={() => setResetting(user.username)}
                                >
                                  Reset password
                                </button>
                              )}
                              {!isMe && local && (
                                <button
                                  type="button"
                                  className="cm-btn cm-btn-destructive-outline cm-btn-sm"
                                  onClick={() => {
                                    setActionError(null);
                                    setConfirmingDelete(user.username);
                                  }}
                                >
                                  Delete
                                </button>
                              )}
                            </>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {adding && (
        <AddUserDialog
          onClose={() => setAdding(false)}
          onCreated={() => {
            setAdding(false);
            setActionError(null);
            refresh();
          }}
        />
      )}
      {resetting && <ResetPasswordDialog username={resetting} onClose={() => setResetting(null)} />}
    </div>
  );
}
