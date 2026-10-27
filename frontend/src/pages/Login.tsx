import { useId, useState } from "react";
import { login, type CurrentUser } from "../api/client";
import { BrandMark, useBranding } from "../branding";

export function Login({ onSuccess }: { onSuccess: (user: CurrentUser) => void }) {
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
      onSuccess(user);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setLoading(false);
    }
  }

  // shadcn's login-01: the brand above a centred card with a title, a line
  // of description, the fields and a full-width button.
  return (
    <div className="cm-login-shell">
      <div className="cm-login-stack">
        <p className="cm-login-brand">
          <BrandMark large />
          <span className="cm-brand-name">{settings.brand_name}</span>
        </p>
        <div className="cm-card cm-login-card">
          <div className="cm-card-header">
            <h1 className="cm-card-title cm-login-title">Sign in</h1>
            <p className="cm-card-description">Enter your username and password to open the dashboard.</p>
          </div>
          <form className="cm-login-form" onSubmit={handleSubmit} noValidate>
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

            <button type="submit" className="cm-btn cm-btn-primary" disabled={loading} data-loading={loading || undefined}>
              {loading ? "Signing in…" : "Sign in"}
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}
