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
npm run dev     # Vite dev server; proxy /api to a running celery-manager
npm run build   # tsc -b && vite build
npm run lint    # oxlint
```

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
