# celery-manager dashboard (frontend)

The React + TypeScript single-page app that `celery-manager` serves at `/`.

**You do not need Node to *use* celery-manager.** This directory is built once
and its output is committed into the Python package at
`src/celery_manager/dashboard/static/`, which is what ships on PyPI and what
`create_app()` mounts. Everything here is for people changing the dashboard.

## The one thing that catches everyone

Editing files in `src/` has no effect on the running dashboard until you
rebuild **and copy the output into the Python package**:

```bash
npm install
npm run build
rm -rf ../src/celery_manager/dashboard/static
cp -R dist ../src/celery_manager/dashboard/static
```

`frontend/dist/` is git-ignored; `src/celery_manager/dashboard/static/` is
tracked on purpose. If you forget the copy, the package quietly ships the
previous build. See [`docs/contributing.md`](../docs/contributing.md).

## Working on it

```bash
npm run dev     # Vite dev server (needs the /api proxy below)
npm run build   # tsc -b && vite build
npm run lint    # oxlint
```

### Dev server against a running backend

`vite.config.ts` has no proxy, and the app calls a relative `api` base
(`src/api/base.ts`), so under `npm run dev` requests go to
`http://localhost:5173/api/...`. Add a proxy to your local `vite.config.ts`
(not committed) that forwards `/api` to the backend, e.g. `run_dashboard.py`
on port 8001:

```ts
export default defineConfig({
  // ...existing config
  server: {
    proxy: {
      '/api': {
        target: 'http://localhost:8001',
        changeOrigin: false, // keep Host as localhost:5173
      },
    },
  },
})
```

Keep `changeOrigin: false`. With `true`, Vite rewrites `Host` to the backend
(`localhost:8001`) while the browser still sends `Origin: http://localhost:5173`,
so the backend's cross-origin check answers login with
`403 cross-origin request refused`. With `false`, `Host` and `Origin` both
read `localhost:5173` and match.

The backend's session cookie is `Secure` by default, and browsers won't send it
back over plain `http://localhost:5173`. Run the backend with
`cookie_secure=False` for local development (the Django demo sets
`CELERY_MANAGER_COOKIE_SECURE = False`).

There is no JS test runner in this project — changes are verified by
type-checking, linting, and driving the built app in a real browser.

## How it fits together

- `src/styles/tokens.css` is the design system. Every colour, space, radius
  and type value is a `--cm-*` custom property, and component files carry no
  ad hoc hex values — that is what makes theming and branding work.
- Themes resolve from a `data-theme` attribute on `<html>` with a
  `prefers-color-scheme` fallback, so an explicit light/dark choice wins in
  both directions. `src/theme.ts` owns that.
- `src/branding.tsx` fetches `/api/settings` once, above the auth check (the
  sign-in screen has to be branded too), and derives the accent's hover,
  pressed, text and tint variants by measuring contrast.
- `src/lib/taskProgress.ts` is the single home for run-timing and progress
  derivations, so the task list and task detail can never disagree about how
  far along a run is.
- Routing uses `HashRouter`, deliberately: the backend serves the SPA from a
  plain static mount with no catch-all, so a real request for `/tasks/abc`
  would 404 before React loaded.
