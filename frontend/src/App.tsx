import { useCallback, useEffect, useState } from "react";
import { HashRouter, Route, Routes } from "react-router-dom";
import { whoami } from "./api/client";
import { Login } from "./pages/Login";
import { TaskList } from "./pages/TaskList";
import { TaskDetail } from "./pages/TaskDetail";
import { ScheduleOccurrences } from "./pages/ScheduleOccurrences";

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

          <button type="submit" onClick={runCheck}>
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
      <Routes>
        <Route path="/" element={<TaskList />} />
        <Route path="/tasks/:taskId" element={<TaskDetail />} />
        <Route path="/schedules/:name" element={<ScheduleOccurrences />} />
      </Routes>
    </HashRouter>
  );
}
