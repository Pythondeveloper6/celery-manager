import { useCallback, useEffect, useState } from "react";
import { HashRouter, Link, Route, Routes } from "react-router-dom";
import { whoami } from "./api/client";
import { BrandMark, useBranding } from "./branding";
import { AppShell } from "./components/AppShell";
import { Login } from "./pages/Login";
import { TaskList } from "./pages/TaskList";
import { TaskDetail } from "./pages/TaskDetail";
import { ScheduleList } from "./pages/ScheduleList";
import { ScheduleOccurrences } from "./pages/ScheduleOccurrences";
import { AuditLog } from "./pages/AuditLog";
import { Settings } from "./pages/Settings";
import { Queues } from "./pages/Queues";

// Unmatched hash paths render a real page inside the shell rather than a
// blank body, which reads as a broken app rather than a bad URL.
function NotFound() {
  return (
    <div className="cm-page">
      <header className="cm-page-header">
        <h1 className="cm-page-title">Page not found</h1>
      </header>

      <div className="cm-card cm-card-flush">
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
        <div className="cm-login-stack">
          <p className="cm-login-brand">
            <BrandMark large />
            <span className="cm-brand-name">{settings.brand_name}</span>
          </p>
          <div className="cm-card cm-login-card">
            <div className="cm-card-header">
              <h1 className="cm-card-title cm-login-title">Couldn&rsquo;t reach the server</h1>
              <p className="cm-card-description">The dashboard loaded, but its API didn&rsquo;t answer.</p>
            </div>
            <div className="cm-login-form">
              <p role="alert" className="cm-error">
                {checkError}
              </p>
              <button type="button" className="cm-btn cm-btn-primary" onClick={runCheck}>
                Try again
              </button>
            </div>
          </div>
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
      <AppShell username={username} onSignedOut={() => setUsername(null)}>
        <Routes>
          <Route path="/" element={<TaskList />} />
          <Route path="/tasks/:taskId" element={<TaskDetail />} />
          <Route path="/schedules" element={<ScheduleList />} />
          <Route path="/schedules/:name" element={<ScheduleOccurrences />} />
          <Route path="/audit" element={<AuditLog />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/queues" element={<Queues />} />
          <Route path="*" element={<NotFound />} />
        </Routes>
      </AppShell>
    </HashRouter>
  );
}
