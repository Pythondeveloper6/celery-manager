import { useCallback, useEffect, useState } from "react";
import { HashRouter, Link, NavLink, Route, Routes } from "react-router-dom";
import { logout, whoami } from "./api/client";
import { BrandMark, ThemeSwitch, useBranding } from "./branding";
import { Login } from "./pages/Login";
import { TaskList } from "./pages/TaskList";
import { TaskDetail } from "./pages/TaskDetail";
import { ScheduleList } from "./pages/ScheduleList";
import { ScheduleOccurrences } from "./pages/ScheduleOccurrences";
import { AuditLog } from "./pages/AuditLog";
import { Settings } from "./pages/Settings";

// The one persistent chrome element across every authenticated screen —
// everything under it (TaskList, TaskDetail, ScheduleList,
// ScheduleOccurrences, AuditLog) still owns its own cm-page-header
// eyebrow/title, so this bar stays deliberately thin: brand mark +
// wordmark, and links for the screens a user can always jump to
// directly. Schedule occurrences are keyed by schedule name
// (`/schedules/:name`) — ScheduleList (Task 18) is what gives "Schedules"
// a single stable destination to link to, closing the gap Task 15 left
// rather than papering over it with a guessed link.
function AppNav({ username, onSignedOut }: { username: string; onSignedOut: () => void }) {
  const { settings } = useBranding();
  const [signingOut, setSigningOut] = useState(false);
  const [signOutError, setSignOutError] = useState<string | null>(null);

  const signOut = () => {
    setSigningOut(true);
    setSignOutError(null);
    logout().then(
      // Unmounts the nav, so there is no need to clear signingOut.
      onSignedOut,
      (err) => {
        setSignOutError(err instanceof Error ? err.message : "Sign out failed.");
        setSigningOut(false);
      },
    );
  };
  return (
    <nav className="cm-app-nav" aria-label="Primary">
      {/* The strip is full-bleed so its rule spans the window; this inner rail
          carries the same max-width and padding as .cm-page, so the wordmark
          sits exactly above the page title rather than off to its left. */}
      <div className="cm-app-nav-inner">
        <span className="cm-app-nav-brand">
          <BrandMark />
          <span className="cm-brand-name">{settings.brand_name}</span>
        </span>
        <div className="cm-app-nav-end">
          <div className="cm-app-nav-links">
            <NavLink to="/" end className="cm-app-nav-link">
              Tasks
            </NavLink>
            <NavLink to="/schedules" className="cm-app-nav-link">
              Schedules
            </NavLink>
            <NavLink to="/audit" className="cm-app-nav-link">
              Audit Log
            </NavLink>
            <NavLink to="/settings" className="cm-app-nav-link">
              Settings
            </NavLink>
          </div>
          {/* The theme switch lives in the persistent chrome, not only on the
              settings screen: it is the one preference someone changes because
              of the room they're in, and hunting for a settings page to do it
              is three clicks too many. The settings screen carries the same
              control, since that is where people go looking for it. */}
          <ThemeSwitch />
          <div className="cm-app-nav-user">
            <span className="cm-app-nav-username" title={username}>
              {username}
            </span>
            <button type="button" className="cm-app-nav-signout" onClick={signOut} disabled={signingOut}>
              {signingOut ? "Signing out…" : "Sign out"}
            </button>
          </div>
        </div>
      </div>
      {signOutError && (
        <p role="alert" className="cm-error cm-app-nav-error">
          {signOutError}
        </p>
      )}
    </nav>
  );
}

// Unmatched hash paths used to render the nav chrome above a completely
// blank body, which reads as a broken app rather than a bad URL. Reuses the
// same cm-page / cm-panel / cm-empty-state shell every other screen uses so
// it stays part of the design system rather than a bolted-on message.
function NotFound() {
  const { settings } = useBranding();
  return (
    <div className="cm-page">
      <header className="cm-page-header">
        <div className="cm-page-header-titles">
          <p className="cm-page-eyebrow">
            <BrandMark />
            {settings.brand_name}
          </p>
          <h1 className="cm-page-title">Page not found</h1>
        </div>
      </header>

      <div className="cm-panel">
        <p className="cm-empty-state">
          There's nothing at this address.{" "}
          <Link to="/" className="cm-back-link">
            Go back to Tasks
          </Link>
          .
        </p>
      </div>
    </div>
  );
}

export function App() {
  const { settings } = useBranding();
  const [username, setUsername] = useState<string | null>(null);
  const [checked, setChecked] = useState(false);
  const [checkError, setCheckError] = useState<string | null>(null);

  const runCheck = useCallback(() => {
    setChecked(false);
    setCheckError(null);
    whoami()
      .then((user) => {
        setUsername(user?.username ?? null);
        setChecked(true);
      })
      .catch((err) => {
        setCheckError(err instanceof Error ? err.message : "Could not reach the server.");
        setChecked(true);
      });
  }, []);

  useEffect(() => {
    runCheck();
  }, [runCheck]);

  if (!checked) return null;

  if (checkError) {
    return (
      <div className="cm-login-shell">
        <div className="cm-login-card">
          <div className="cm-login-header">
            <h1>
              <BrandMark large />
              {settings.brand_name}
            </h1>
            <p className="cm-login-tagline">Connection problem</p>
            <div className="cm-pulse-rule" aria-hidden="true" />
          </div>

          <p role="alert" className="cm-error">
            {checkError}
          </p>

          <button type="button" className="cm-btn cm-btn-solid" onClick={runCheck}>
            Retry
          </button>
        </div>
      </div>
    );
  }

  if (!username) return <Login onSuccess={setUsername} />;

  // HashRouter, not BrowserRouter: the backend serves the built SPA via a
  // plain StaticFiles(html=True) mount with no catch-all fallback route, so
  // a real (non-JS-driven) request for e.g. /tasks/abc 404s at the server
  // before React ever runs. Routing on the URL fragment keeps every
  // deep link, refresh, and bookmark resolving to the same "/" request the
  // server already serves, while React Router still owns everything after
  // the "#".
  return (
    <HashRouter>
      <AppNav username={username} onSignedOut={() => setUsername(null)} />
      <Routes>
        <Route path="/" element={<TaskList />} />
        <Route path="/tasks/:taskId" element={<TaskDetail />} />
        <Route path="/schedules" element={<ScheduleList />} />
        <Route path="/schedules/:name" element={<ScheduleOccurrences />} />
        <Route path="/audit" element={<AuditLog />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="*" element={<NotFound />} />
      </Routes>
    </HashRouter>
  );
}
