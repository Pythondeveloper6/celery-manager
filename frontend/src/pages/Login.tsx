import { useId, useState } from "react";
import type { ReactNode } from "react";
import { hostLoginHref, login, logout, type AuthOptions, type CurrentUser } from "../api/client";
import { BrandMark, useBranding } from "../branding";

// shadcn's login-01: the brand above a centred card with a title, a line of
// description and the card's content.
function AuthScreen({ title, description, children }: { title: string; description: string; children: ReactNode }) {
  // Branding is already in context by the time this renders (BrandingProvider
  // wraps App in main.tsx, above the authentication check) — that is the whole
  // reason GET /api/settings is unauthenticated. If it never arrives, this
  // shows the built-in wordmark and the form still works.
  const { settings } = useBranding();
  return (
    <div className="cm-login-shell">
      <div className="cm-login-stack">
        <p className="cm-login-brand">
          <BrandMark large />
          <span className="cm-brand-name">{settings.brand_name}</span>
        </p>
        <div className="cm-card cm-login-card">
          <div className="cm-card-header">
            <h1 className="cm-card-title cm-login-title">{title}</h1>
            <p className="cm-card-description">{description}</p>
          </div>
          {children}
        </div>
      </div>
    </div>
  );
}

/** The way in through the host app: a link to its login page, or, without
 *  one configured, a nudge to sign in there and come back. */
function HostSignIn({ options, onRetry, primary }: { options: AuthOptions; onRetry: () => void; primary: boolean }) {
  if (options.login_url) {
    return (
      <a className={`cm-btn ${primary ? "cm-btn-primary" : "cm-btn-outline"}`} href={hostLoginHref(options.login_url)}>
        Sign in with {options.provider_name}
      </a>
    );
  }
  return (
    <>
      <p className="cm-field-hint">
        Sign in to {options.provider_name} in this browser, then come back to this page.
      </p>
      <button type="button" className={`cm-btn ${primary ? "cm-btn-primary" : "cm-btn-outline"}`} onClick={onRetry}>
        I&rsquo;ve signed in
      </button>
    </>
  );
}

export function Login({
  options,
  onSuccess,
  onRetry,
}: {
  options: AuthOptions;
  onSuccess: (user: CurrentUser) => void;
  // Re-checks whoami, for someone who has just signed in to the host app.
  onRetry: () => void;
}) {
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

  if (!options.local_login) {
    return (
      <AuthScreen title="Sign in" description={`This dashboard uses your ${options.provider_name} account.`}>
        <div className="cm-login-form">
          <HostSignIn options={options} onRetry={onRetry} primary />
        </div>
      </AuthScreen>
    );
  }

  return (
    <AuthScreen title="Sign in" description="Enter your username and password to open the dashboard.">
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
      {/* Both ways in: the host app, and a local account (often a
          break-glass admin) for when the host is down. */}
      {options.external && options.login_url && (
        <div className="cm-login-form">
          <p className="cm-login-divider">or</p>
          <HostSignIn options={options} onRetry={onRetry} primary={false} />
        </div>
      )}
    </AuthScreen>
  );
}

/** Signed in to the host app, but its role mapping gives this person no
 *  access (whoami's 403). The server's sentence names the account, so they
 *  can tell whether they are signed in as the wrong person. */
export function NoAccess({ message, options, onRetry }: { message: string; options: AuthOptions; onRetry: () => void }) {
  const [signingOut, setSigningOut] = useState(false);

  function signOutOfHost(event: React.MouseEvent<HTMLAnchorElement>) {
    const href = event.currentTarget.href;
    event.preventDefault();
    setSigningOut(true);
    // Drop any local session too, or signing out of the host would land
    // them back here as whatever local account this browser last used.
    void logout()
      .catch(() => undefined)
      .finally(() => window.location.assign(href));
  }

  return (
    <AuthScreen title="No access" description="You are signed in, but not to an account that can open this dashboard.">
      <div className="cm-login-form">
        <p role="alert" className="cm-error">
          {message}
        </p>
        <p className="cm-field-hint">Ask whoever runs {options.provider_name} to give your account a role here.</p>
        {options.logout_url && (
          <a className="cm-btn cm-btn-outline" href={options.logout_url} onClick={signOutOfHost} aria-disabled={signingOut || undefined}>
            {signingOut ? "Signing out…" : `Sign out of ${options.provider_name}`}
          </a>
        )}
        <button type="button" className="cm-btn cm-btn-primary" onClick={onRetry}>
          Try again
        </button>
      </div>
    </AuthScreen>
  );
}
