import { useId, useState } from "react";
import { login } from "../api/client";
import { BrandMark, useBranding } from "../branding";

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
          <p className="cm-login-tagline">Queue control</p>
          <div className="cm-pulse-rule" aria-hidden="true" />
        </div>

        <div className="cm-field">
          <label htmlFor={usernameId}>Username</label>
          <input
            id={usernameId}
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

        <button type="submit" disabled={loading} data-loading={loading || undefined}>
          {loading ? "Signing in…" : "Sign in"}
        </button>

        <p className="cm-login-meta">Self-hosted &middot; session-based auth</p>
      </form>
    </div>
  );
}
