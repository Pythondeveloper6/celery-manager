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

// The one persistent chrome element across every authenticated screen: a
// left rail with the brand (the only place it appears — pages carry no
// eyebrow), the screens a user can always jump to directly, and the account
// cluster at the foot. Below 900px CSS turns it into a top bar. Schedule
// occurrences are keyed by schedule name (`/schedules/:name`), so
// "Schedules" links to the list that leads there.
function AppRail({ username, onSignedOut }: { username: string; onSignedOut: () => void }) {
  const { settings } = useBranding();
  const [signingOut, setSigningOut] = useState(false);
  const [signOutError, setSignOutError] = useState<string | null>(null);

  const signOut = () => {
    setSigningOut(true);
    setSignOutError(null);
    logout().then(
      // Unmounts the rail, so there is no need to clear signingOut.
      onSignedOut,
      (err) => {
        setSignOutError(err instanceof Error ? err.message : "Sign out failed.");
        setSigningOut(false);
      },
    );
  };
  return (
    <nav className="cm-rail" aria-label="Primary">
      <span className="cm-rail-brand">
        <BrandMark />
        <span className="cm-brand-name">{settings.brand_name}</span>
      </span>
      <ul className="cm-rail-links">
        <li>
          <NavLink to="/" end className="cm-rail-link">
            Tasks
          </NavLink>
        </li>
        <li>
          <NavLink to="/schedules" className="cm-rail-link">
            Schedules
          </NavLink>
        </li>
        <li>
          <NavLink to="/audit" className="cm-rail-link">
            Audit log
          </NavLink>
        </li>
        <li>
          <NavLink to="/settings" className="cm-rail-link">
            Settings
          </NavLink>
        </li>
      </ul>
      <div className="cm-rail-foot">
        <span className="cm-rail-user" title={`Signed in as ${username}`}>
          {username}
        </span>
        <div className="cm-rail-foot-row">
          {/* The theme switch lives in the persistent chrome as well as on the
              settings screen: it is the one preference someone changes
              because of the room they're in. */}
          <ThemeSwitch />
          <button type="button" className="cm-rail-signout" onClick={signOut} disabled={signingOut}>
            {signingOut ? "Signing out…" : "Sign out"}
          </button>
        </div>
        {signOutError && (
          <p role="alert" className="cm-error">
            {signOutError}
          </p>
        )}
      </div>
    </nav>
  );
}

// Unmatched hash paths render a real page inside the shell rather than a
// blank body, which reads as a broken app rather than a bad URL.
function NotFound() {
  return (
    <div className="cm-page">
      <header className="cm-page-header">
        <h1 className="cm-page-title">Page not found</h1>
      </header>

      <div className="cm-panel">
        <p className="cm-empty-state">
          There&rsquo;s nothing at this address.{" "}
          <Link to="/" className="cm-link">
            Go to task runs
          </Link>
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
            <p className="cm-login-sub">Couldn&rsquo;t reach the server.</p>
          </div>

          <p role="alert" className="cm-error">
            {checkError}
          </p>

          <button type="button" className="cm-btn cm-btn-solid" onClick={runCheck}>
            Try again
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
      <div className="cm-shell">
        <AppRail username={username} onSignedOut={() => setUsername(null)} />
        <main className="cm-main">
          <Routes>
            <Route path="/" element={<TaskList />} />
            <Route path="/tasks/:taskId" element={<TaskDetail />} />
            <Route path="/schedules" element={<ScheduleList />} />
            <Route path="/schedules/:name" element={<ScheduleOccurrences />} />
            <Route path="/audit" element={<AuditLog />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="*" element={<NotFound />} />
          </Routes>
        </main>
      </div>
    </HashRouter>
  );
}
