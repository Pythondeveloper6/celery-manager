import { useEffect, useState } from "react";
import { whoami } from "./api/client";
import { Login } from "./pages/Login";

export function App() {
  const [username, setUsername] = useState<string | null>(null);
  const [checked, setChecked] = useState(false);

  useEffect(() => {
    whoami().then((user) => {
      setUsername(user?.username ?? null);
      setChecked(true);
    });
  }, []);

  if (!checked) return null;
  if (!username) return <Login onSuccess={setUsername} />;

  return (
    <div className="cm-app-placeholder">
      <p>
        Signed in as <strong>{username}</strong> &mdash; dashboard screens land in Tasks 13-15.
      </p>
    </div>
  );
}
