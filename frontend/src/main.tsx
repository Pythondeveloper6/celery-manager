import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { BrandingProvider } from "./branding";

// Self-hosted font files, latin subset only (no external CDN calls — this
// dashboard is expected to run on internal/offline networks).
import "@fontsource/schibsted-grotesk/latin-400.css";
import "@fontsource/schibsted-grotesk/latin-500.css";
import "@fontsource/schibsted-grotesk/latin-600.css";
import "@fontsource/schibsted-grotesk/latin-700.css";
import "@fontsource/jetbrains-mono/latin-400.css";
import "@fontsource/jetbrains-mono/latin-500.css";

import "./styles/tokens.css";

// BrandingProvider sits above <App/>, not inside it: the login screen is
// branded too, and App decides between the login screen and the dashboard.
// One fetch of /api/settings happens here and nowhere else.
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrandingProvider>
      <App />
    </BrandingProvider>
  </React.StrictMode>
);
