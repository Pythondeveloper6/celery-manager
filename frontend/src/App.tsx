import { useCallback, useEffect, useState } from "react";
import { whoami } from "./api/client";
import { Login } from "./pages/Login";

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

  return (
    <div className="cm-app-placeholder">
      <p>
        Signed in as <strong>{username}</strong> &mdash; dashboard screens land in Tasks 13-15.
      </p>
    </div>
  );
}
