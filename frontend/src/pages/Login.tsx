import { useId, useState } from "react";
import { login } from "../api/client";
import { BrandMark, useBranding } from "../branding";

// Twelve bars, oldest to newest: mostly quiet successes, one failure, one
// retry, one still running and one waiting. Heights in px, on the same 6–32
// scale the live strip uses.
const MOTIF: [string, number][] = [
  ["succeeded", 14],
  ["succeeded", 20],
  ["succeeded", 11],
  ["failed", 26],
  ["succeeded", 16],
  ["retried", 22],
  ["succeeded", 13],
  ["succeeded", 18],
  ["succeeded", 9],
  ["succeeded", 15],
  ["started", 30],
  ["queued", 6],
];

export function Login({ onSuccess }: { onSuccess: (username: string) => void }) {
  // Branding is already in context by the time this renders (BrandingProvider
  // wraps App in main.tsx, above the authentication check) — that is the whole
  // reason GET /api/settings is unauthenticated. If it never arrives, this
  // shows the built-in wordmark and the form still works.
  const { settings } = useBranding();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const usernameId = useId();
  const passwordId = useId();
  const errorId = useId();

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      const user = await login(username, password);
      onSuccess(user.username);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="cm-login-shell">
      <form className="cm-login-card" onSubmit={handleSubmit} noValidate>
        <div className="cm-login-header">
          <h1>
            <BrandMark large />
            {settings.brand_name}
          </h1>
          {/* The run strip from the task list, as a still motif. */}
          <div className="cm-login-motif" aria-hidden="true">
            {MOTIF.map(([status, height], i) => (
              <span key={i} data-status={status} style={{ height }} />
            ))}
          </div>
        </div>

        <div className="cm-field">
          <label htmlFor={usernameId}>Username</label>
          <input
            id={usernameId}
            className="cm-input"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoFocus
            autoComplete="username"
            autoCapitalize="none"
            spellCheck={false}
            disabled={loading}
            required
          />
        </div>

        <div className="cm-field">
          <label htmlFor={passwordId}>Password</label>
          <input
            id={passwordId}
            className="cm-input"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            aria-describedby={error ? errorId : undefined}
            aria-invalid={error ? true : undefined}
            disabled={loading}
            required
          />
        </div>

        {error && (
          <p role="alert" id={errorId} className="cm-error">
            {error}
          </p>
        )}

        <button type="submit" className="cm-btn cm-btn-solid" disabled={loading} data-loading={loading || undefined}>
          {loading ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
