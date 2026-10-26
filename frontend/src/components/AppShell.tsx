import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import type { ComponentType, ReactNode } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { logout } from "../api/client";
import { BrandMark, ThemeSwitch, useBranding } from "../branding";
import {
  IconCalendarClock,
  IconChartLine,
  IconLayers,
  IconListChecks,
  IconLogOut,
  IconPanelLeft,
  IconScrollText,
  IconServer,
  IconSettings,
  IconX,
} from "./icons";

/* ==========================================================================
   App shell: a collapsible sidebar (shadcn's Sidebar, collapsible="icon")
   beside a slim header bar and the page.

   Desktop: the sidebar is 16rem with labels, or a 3rem icon rail whose items
   show their label in a tooltip. The choice persists per browser. Below
   768px it is an off-canvas sheet over the page instead, opened by the same
   trigger. Cmd/Ctrl+B toggles whichever applies. Schedule occurrences are
   keyed by schedule name (`/schedules/:name`), so "Schedules" links to the
   list that leads there.
   ========================================================================== */

const SIDEBAR_STORAGE_KEY = "cm-sidebar";
const mobileQuery = window.matchMedia("(max-width: 767px)");

const NAV_ITEMS: { to: string; end?: boolean; label: string; icon: ComponentType<{ className?: string }> }[] = [
  { to: "/", end: true, label: "Tasks", icon: IconListChecks },
  { to: "/schedules", label: "Schedules", icon: IconCalendarClock },
  { to: "/audit", label: "Audit log", icon: IconScrollText },
  { to: "/insights", label: "Insights", icon: IconChartLine },
  { to: "/workers", label: "Workers", icon: IconServer },
  { to: "/queues", label: "Queues", icon: IconLayers },
  { to: "/settings", label: "Settings", icon: IconSettings },
];

const SHORTCUT_LABEL = /Mac|iPhone|iPad/.test(navigator.userAgent) ? "⌘B" : "Ctrl+B";

// Same guard as the theme store: storage can throw outright in private modes.
function readCollapsed(): boolean {
  try {
    return window.localStorage.getItem(SIDEBAR_STORAGE_KEY) === "collapsed";
  } catch {
    return false;
  }
}

function writeCollapsed(collapsed: boolean): void {
  try {
    window.localStorage.setItem(SIDEBAR_STORAGE_KEY, collapsed ? "collapsed" : "expanded");
  } catch {
    // Not remembered across reloads; this session still honours it.
  }
}

function subscribeMobile(listener: () => void): () => void {
  mobileQuery.addEventListener("change", listener);
  return () => mobileQuery.removeEventListener("change", listener);
}

function useIsMobile(): boolean {
  return useSyncExternalStore(subscribeMobile, () => mobileQuery.matches);
}

function sectionLabel(pathname: string): string {
  if (pathname.startsWith("/schedules")) return "Schedules";
  if (pathname.startsWith("/audit")) return "Audit log";
  if (pathname.startsWith("/workers")) return "Workers";
  if (pathname.startsWith("/settings")) return "Settings";
  if (pathname.startsWith("/queues")) return "Queues";
  if (pathname === "/" || pathname.startsWith("/tasks")) return "Tasks";
  return "";
}

export function AppShell({
  username,
  onSignedOut,
  children,
}: {
  username: string;
  onSignedOut: () => void;
  children: ReactNode;
}) {
  const { settings } = useBranding();
  const { pathname } = useLocation();
  const isMobile = useIsMobile();
  const [collapsed, setCollapsed] = useState(readCollapsed);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [signingOut, setSigningOut] = useState(false);
  const [signOutError, setSignOutError] = useState<string | null>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const wasOpenRef = useRef(false);

  const sheetOpen = isMobile && mobileOpen;

  const toggle = useCallback(() => {
    if (mobileQuery.matches) setMobileOpen((open) => !open);
    else setCollapsed((value) => !value);
  }, []);

  useEffect(() => writeCollapsed(collapsed), [collapsed]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key.toLowerCase() !== "b" || !(event.metaKey || event.ctrlKey) || event.altKey || event.shiftKey) return;
      event.preventDefault();
      toggle();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [toggle]);

  // The sheet closes on navigation, and on leaving the mobile breakpoint.
  useEffect(() => setMobileOpen(false), [pathname, isMobile]);

  // Focus goes into the sheet when it opens and back to the trigger when it
  // closes. Done after commit, once the page behind is no longer inert.
  useEffect(() => {
    if (sheetOpen) closeRef.current?.focus();
    else if (wasOpenRef.current) triggerRef.current?.focus();
    wasOpenRef.current = sheetOpen;
  }, [sheetOpen]);

  useEffect(() => {
    if (!sheetOpen) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMobileOpen(false);
    };
    document.addEventListener("keydown", onKey);
    const { overflow } = document.body.style;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = overflow;
    };
  }, [sheetOpen]);

  const signOut = () => {
    setSigningOut(true);
    setSignOutError(null);
    logout().then(
      // Unmounts the shell, so there is no need to clear signingOut.
      onSignedOut,
      (err) => {
        setSignOutError(err instanceof Error ? err.message : "Sign out failed.");
        setSigningOut(false);
      },
    );
  };

  return (
    <div className="cm-shell">
      <aside
        id="cm-sidebar"
        className="cm-sidebar"
        data-state={collapsed ? "collapsed" : "expanded"}
        data-mobile-open={sheetOpen || undefined}
        {...(sheetOpen ? { role: "dialog", "aria-modal": true, "aria-label": "Navigation" } : {})}
      >
        <div className="cm-sidebar-header">
          <span className="cm-sidebar-brand">
            <BrandMark />
            <span className="cm-brand-name">{settings.brand_name}</span>
          </span>
          <button
            ref={closeRef}
            type="button"
            className="cm-btn cm-btn-ghost cm-btn-icon cm-sidebar-close"
            aria-label="Close navigation"
            onClick={() => setMobileOpen(false)}
          >
            <IconX />
          </button>
        </div>

        <nav className="cm-sidebar-content" aria-label="Primary">
          <ul className="cm-sidebar-menu">
            {NAV_ITEMS.map(({ to, end, label, icon: Icon }) => (
              <li key={to} className="cm-sidebar-item">
                <NavLink to={to} end={end} className="cm-sidebar-button">
                  <Icon />
                  <span className="cm-sidebar-label">{label}</span>
                </NavLink>
                <span className="cm-tooltip" aria-hidden="true">
                  {label}
                </span>
              </li>
            ))}
          </ul>
        </nav>

        <div className="cm-sidebar-footer">
          {/* The theme switch lives in the persistent chrome as well as on the
              settings screen: it is the one preference someone changes
              because of the room they're in. */}
          <ThemeSwitch />
          <div className="cm-sidebar-item">
            <button type="button" className="cm-sidebar-button" onClick={signOut} disabled={signingOut}>
              <IconLogOut />
              <span className="cm-sidebar-label">{signingOut ? "Signing out…" : "Sign out"}</span>
            </button>
            <span className="cm-tooltip" aria-hidden="true">
              Sign out
            </span>
          </div>
          {signOutError && (
            <p role="alert" className="cm-error cm-sidebar-error">
              {signOutError}
            </p>
          )}
          <hr className="cm-separator" />
          <div className="cm-sidebar-item cm-sidebar-user">
            <span className="cm-avatar" aria-hidden="true">
              {username.charAt(0).toUpperCase()}
            </span>
            <span className="cm-sidebar-label cm-sidebar-user-text">
              <span className="cm-sidebar-user-name">{username}</span>
              <span className="cm-sidebar-user-note">Signed in</span>
            </span>
            <span className="cm-tooltip" aria-hidden="true">
              Signed in as {username}
            </span>
          </div>
        </div>
      </aside>

      <div className="cm-sidebar-overlay" data-open={sheetOpen || undefined} aria-hidden="true" onClick={() => setMobileOpen(false)} />

      <div className="cm-inset" inert={sheetOpen}>
        <header className="cm-topbar">
          <button
            ref={triggerRef}
            type="button"
            className="cm-btn cm-btn-ghost cm-btn-icon cm-sidebar-trigger"
            aria-label="Toggle sidebar"
            aria-controls="cm-sidebar"
            aria-expanded={isMobile ? sheetOpen : !collapsed}
            title={`Toggle sidebar (${SHORTCUT_LABEL})`}
            onClick={toggle}
          >
            <IconPanelLeft />
          </button>
          <span className="cm-separator cm-separator-vertical" aria-hidden="true" />
          <span className="cm-topbar-title">{sectionLabel(pathname)}</span>
        </header>
        <main className="cm-main">{children}</main>
      </div>
    </div>
  );
}
