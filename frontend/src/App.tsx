import { useCallback, useEffect, useState } from "react";
import { HashRouter, Link, NavLink, Route, Routes } from "react-router-dom";
import { whoami } from "./api/client";
import { Login } from "./pages/Login";
import { TaskList } from "./pages/TaskList";
import { TaskDetail } from "./pages/TaskDetail";
import { ScheduleList } from "./pages/ScheduleList";
import { ScheduleOccurrences } from "./pages/ScheduleOccurrences";
import { AuditLog } from "./pages/AuditLog";

// The one persistent chrome element across every authenticated screen —
// everything under it (TaskList, TaskDetail, ScheduleList,
// ScheduleOccurrences, AuditLog) still owns its own cm-page-header
// eyebrow/title, so this bar stays deliberately thin: brand mark +
// wordmark, and links for the screens a user can always jump to
// directly. Schedule occurrences are keyed by schedule name
// (`/schedules/:name`) — ScheduleList (Task 18) is what gives "Schedules"
// a single stable destination to link to, closing the gap Task 15 left
// rather than papering over it with a guessed link.
function AppNav() {
  return (
    <nav className="cm-app-nav" aria-label="Primary">
      <span className="cm-app-nav-brand">
        <span className="cm-brand-mark" aria-hidden="true" />
        celery-manager
      </span>
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
      </div>
    </nav>
  );
}

// Unmatched hash paths used to render the nav chrome above a completely
// blank body, which reads as a broken app rather than a bad URL. Reuses the
// same cm-page / cm-panel / cm-empty-state shell every other screen uses so
// it stays part of the design system rather than a bolted-on message.
function NotFound() {
  return (
    <div className="cm-page">
      <header className="cm-page-header">
        <div className="cm-page-header-titles">
          <p className="cm-page-eyebrow">
            <span className="cm-brand-mark" aria-hidden="true" />
            celery-manager
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
              <span className="cm-brand-mark" aria-hidden="true" />
              celery-manager
            </h1>
            <p className="cm-login-tagline">Connection problem</p>
            <div className="cm-pulse-rule" aria-hidden="true" />
          </div>

          <p role="alert" className="cm-error">
            {checkError}
          </p>

          <button type="button" onClick={runCheck}>
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
      <AppNav />
      <Routes>
        <Route path="/" element={<TaskList />} />
        <Route path="/tasks/:taskId" element={<TaskDetail />} />
        <Route path="/schedules" element={<ScheduleList />} />
        <Route path="/schedules/:name" element={<ScheduleOccurrences />} />
        <Route path="/audit" element={<AuditLog />} />
        <Route path="*" element={<NotFound />} />
      </Routes>
    </HashRouter>
  );
}
