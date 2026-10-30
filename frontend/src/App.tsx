import { useCallback, useEffect, useState } from "react";
import { HashRouter, Link, Route, Routes } from "react-router-dom";
import {
  DEFAULT_AUTH_OPTIONS,
  getAuthOptions,
  NoAccessError,
  whoami,
  type AuthOptions,
  type CurrentUser,
} from "./api/client";
import { AuthOptionsContext, SourceContext } from "./lib/auth";
import { RoleContext, UsernameContext } from "./lib/role";
import { BrandMark, useBranding } from "./branding";
import { AppShell } from "./components/AppShell";
import { Login, NoAccess } from "./pages/Login";
import { TaskList } from "./pages/TaskList";
import { TaskDetail } from "./pages/TaskDetail";
import { DeadLetters } from "./pages/DeadLetters";
import { ScheduleList } from "./pages/ScheduleList";
import { ScheduleOccurrences } from "./pages/ScheduleOccurrences";
import { AuditLog } from "./pages/AuditLog";
import { Settings } from "./pages/Settings";
import { Insights } from "./pages/Insights";
import { TaskOverview } from "./pages/TaskOverview";
import { Workers } from "./pages/Workers";
import { Queues } from "./pages/Queues";
import { QueueMessages } from "./pages/QueueMessages";
import { HeldTasks } from "./pages/HeldTasks";
import { RabbitDeadLetters } from "./pages/RabbitDeadLetters";
import { Users } from "./pages/Users";
import { Alerts } from "./pages/Alerts";
import { Operations } from "./pages/Operations";
import { installOperatorSafeguards } from "./lib/safeguards";

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
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [authOptions, setAuthOptions] = useState<AuthOptions>(DEFAULT_AUTH_OPTIONS);
  const [checked, setChecked] = useState(false);
  const [checkError, setCheckError] = useState<string | null>(null);
  // The host app signed this person in but gave them no role here.
  const [noAccess, setNoAccess] = useState<string | null>(null);

  useEffect(() => installOperatorSafeguards(authOptions.environment || "", authOptions.sensitive_actions || []), [authOptions]);

  const runCheck = useCallback(() => {
    setChecked(false);
    setCheckError(null);
    setNoAccess(null);
    // Together, so the login screen knows whether to show the password form
    // or a "Sign in with <host>" link before it first paints.
    Promise.all([whoami(), getAuthOptions()])
      .then(([user, options]) => {
        setUser(user);
        setAuthOptions(options);
        setChecked(true);
      })
      .catch(async (err) => {
        if (err instanceof NoAccessError) {
          setAuthOptions(await getAuthOptions());
          setNoAccess(err.message);
        } else {
          setCheckError(err instanceof Error ? err.message : "Could not reach the server.");
        }
        setChecked(true);
      });
  }, []);

  useEffect(() => {
    runCheck();
  }, [runCheck]);

  if (!checked) return null;

  if (noAccess) return <NoAccess message={noAccess} options={authOptions} onRetry={runCheck} />;

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

  if (!user) return <Login options={authOptions} onSuccess={setUser} onRetry={runCheck} />;

  // HashRouter, not BrowserRouter: the backend serves the built SPA via a
  // plain StaticFiles(html=True) mount with no catch-all fallback route, so
  // a real (non-JS-driven) request for e.g. /tasks/abc 404s at the server
  // before React ever runs. Routing on the URL fragment keeps every
  // deep link, refresh, and bookmark resolving to the same "/" request the
  // server already serves, while React Router still owns everything after
  // the "#".
  return (
    <HashRouter>
      <AuthOptionsContext.Provider value={authOptions}>
      <SourceContext.Provider value={user.source}>
      <RoleContext.Provider value={user.role}>
      {authOptions.tenant_scoped && <p className="cm-notice" role="status">Tenant: {user.tenant_id ?? "scoped"} · Read-only task history. Global operations are unavailable.</p>}
        {/* "You" on the Users page means your local account. Someone the
            host signed in is not the local account that shares their name. */}
        <UsernameContext.Provider value={user.source === "local" ? user.username : null}>
        <AppShell username={user.username} role={user.role} onSignedOut={() => setUser(null)}>
          <Routes>
            <Route path="/" element={<TaskList />} />
            <Route path="/tasks/:taskId" element={<TaskDetail />} />
            <Route path="/dead-letters" element={<DeadLetters />} />
            <Route path="/schedules" element={<ScheduleList />} />
            <Route path="/schedules/:name" element={<ScheduleOccurrences />} />
            <Route path="/audit" element={<AuditLog />} />
            <Route path="/insights" element={<Insights />} />
            <Route path="/task-types/:taskName" element={<TaskOverview />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="/workers" element={<Workers />} />
            <Route path="/queues" element={<Queues />} />
            <Route path="/queues/:name" element={<QueueMessages />} />
            <Route path="/held" element={<HeldTasks />} />
            <Route path="/rabbitmq/dead-letters" element={<RabbitDeadLetters />} />
            <Route path="/users" element={<Users />} />
            <Route path="/alerts" element={<Alerts />} />
              <Route path="/operations" element={<Operations />} />
            <Route path="*" element={<NotFound />} />
          </Routes>
        </AppShell>
        </UsernameContext.Provider>
      </RoleContext.Provider>
      </SourceContext.Provider>
      </AuthOptionsContext.Provider>
    </HashRouter>
  );
}
