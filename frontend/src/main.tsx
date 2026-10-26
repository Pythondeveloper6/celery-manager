import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { BrandingProvider } from "./branding";

// Self-hosted variable fonts (no external CDN calls — this dashboard is
// expected to run on internal/offline networks). Each subset is a separate
// file behind a unicode-range, so browsers fetch only latin in practice.
import "@fontsource-variable/geist/wght.css";
import "@fontsource-variable/geist-mono/wght.css";

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
