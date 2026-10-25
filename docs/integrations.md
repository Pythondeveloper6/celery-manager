# Integrating with your app

`create_app(config)` returns a self-contained ASGI application (a FastAPI app)
that serves the dashboard at `/`, the REST API at `/api/*` and the live-update
stream at `/api/stream`. You can run it on its own, or **mount it under a
prefix** (say `/celery-manager`) inside the web app you already have. The
dashboard's URLs are all relative, so the same build works at the root and under
any prefix.

Runnable projects for each framework live in the repository:
[`examples/fastapi_demo`](https://github.com/pythondeveloper6/celery-manager/tree/main/examples/fastapi_demo),
[`examples/flask_demo`](https://github.com/pythondeveloper6/celery-manager/tree/main/examples/flask_demo) and
[`examples/django_demo`](https://github.com/pythondeveloper6/celery-manager/tree/main/examples/django_demo).

!!! note "Always use the trailing slash"
    Open the dashboard at `/celery-manager/`. Its asset and API URLs are relative
    to the page, so without the slash they would resolve against the host's
    root. Starlette (so FastAPI) redirects `/celery-manager` to
    `/celery-manager/` for you; check that your own proxy or framework does too.

## FastAPI

Mount it like any other ASGI sub-application:

```python
from fastapi import FastAPI

from celery_manager.api.app import create_app
from celery_manager.config import CeleryManagerConfig

dashboard, _session_factory = create_app(CeleryManagerConfig(
    celery_app=celery_app,
    db_url="sqlite:///celery_manager.db",
    secret_key=SECRET,   # a private random value, not "change-me"
))

app = FastAPI()
# ... your own routes ...
app.mount("/celery-manager", dashboard)
```

Run with `uvicorn main:app` and open `http://localhost:8000/celery-manager/`.
Your routes keep working; the mount only claims its own prefix.

`create_app()` does all of its setup immediately and registers no
startup/lifespan handlers, so it doesn't matter that a mounted sub-app's
lifespan never runs.

## Flask (WSGI)

Flask is WSGI and the dashboard is ASGI. Adapt it with
[`a2wsgi`](https://github.com/abersheeran/a2wsgi) and route the prefix with
werkzeug's `DispatcherMiddleware`:

Install the extra with `celery-manager[flask]` (see the
[Quickstart](quickstart.md#install)); it adds Flask and `a2wsgi`.

```python
from a2wsgi import ASGIMiddleware
from flask import Flask
from werkzeug.middleware.dispatcher import DispatcherMiddleware

from celery_manager.api.app import create_app

app = Flask(__name__)
dashboard, _ = create_app(config)

app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {
    "/celery-manager": ASGIMiddleware(dashboard),
})
```

Serve `app` with any WSGI server (`flask run`, gunicorn, waitress). Login,
the task list and every other REST call work through the adapter.

!!! warning "Live updates (SSE) do not work well through a2wsgi"
    We tested `/api/stream` through a2wsgi on the werkzeug threaded server.
    Events arrive in real time (they are not buffered), but every open stream
    holds a WSGI worker thread for as long as the tab is open, and a2wsgi never
    notices when the browser disconnects, so each closed tab or reload leaks a
    subscriber and its queue until the process restarts.

    The dashboard works without it (the task list polls), so the Flask demo
    answers `/api/stream` with `204 No Content` by wrapping the dashboard in a
    tiny ASGI function (see `without_sse` in `examples/flask_demo/app.py`; the
    browser's `EventSource` stops and does not retry on a 204). If you want push
    updates, run the dashboard as its own uvicorn process and put it behind the
    same reverse proxy path as your Flask app (see
    [Behind a reverse proxy](#behind-a-reverse-proxy)).

## Django

Django's WSGI `urlpatterns` can't serve an ASGI app directly. Two options:

- **Standalone process (recommended).** Run the dashboard with uvicorn next to
  your Django server, the way the bundled demo does:
  `examples/django_demo/run_dashboard.py` builds the `create_app(...)` app and
  runs it on its own port. Put it under a path of your main domain with a
  reverse proxy (below) if you don't want a second hostname.
- **In-process.** If your project serves itself over ASGI, wrap Django's ASGI
  application and the dashboard in a small dispatcher (for example a Starlette
  `Mount("/celery-manager", dashboard)` next to `Mount("/", django_asgi_app)`).
  On plain WSGI the Flask recipe above (`a2wsgi` + `DispatcherMiddleware`) works
  with the same SSE caveat.

## The worker, beat and events listener

Mounting only affects the web process. The rest of the setup is unchanged
and each process must use the **same `db_url`**:

| Process | Command | Notes |
|---|---|---|
| Web | `uvicorn main:app` / `flask run` | Calls `create_app()`; needs the real `secret_key`. |
| Worker | `celery -A yourapp worker` | Must call `celery_manager.setup(config)` at import time of the module that defines your tasks, or `@track` silently does nothing. Needs no `secret_key`. |
| Beat | `celery -A yourapp beat` | Only if you have periodic tasks. |
| Events listener | `celery-manager events --celery-app yourapp:app --db-url sqlite:///celery_manager.db` | Records every task, including undecorated ones, and the queued/started transitions. |

```python
# tasks.py (imported by the worker)
import celery_manager
from celery_manager.config import CeleryManagerConfig

celery_manager.setup(CeleryManagerConfig(celery_app=app, db_url=DB_URL))
```

The events listener needs Celery task events switched on; see
[Required Celery settings](quickstart.md#required-celery-settings).

Create a dashboard user with `celery_manager.auth.users.create_user`; both demos
include a `create_user.py` script.

## Cookies, `cookie_secure` and the mount prefix

The session cookie is named `cm_session` (not `session`, so it doesn't collide
with Flask's or Starlette's own) and is scoped to the mount prefix: its `Path`
is the ASGI `root_path`, so the browser doesn't send it to the rest of your app.

`cookie_secure=True` (the default) marks the cookie `Secure`, so the browser only
sends it over HTTPS. Behind TLS, leave it alone. For local plain-HTTP
development, set `cookie_secure=False` (see
[Configuration](configuration.md#cookie_secure-and-why-it-defaults-to-true)),
otherwise login appears to succeed and the next request is unauthenticated.

## Behind a reverse proxy

To serve the dashboard from its own process under a path of your main site,
have the proxy strip the prefix and tell uvicorn what it was:

```bash
uvicorn yourmodule:dashboard --port 8001 --root-path /celery-manager
```

```nginx
location = /celery-manager { return 308 /celery-manager/; }
location /celery-manager/ {
    proxy_pass http://127.0.0.1:8001/;   # trailing slash strips the prefix
    proxy_set_header Host $http_host;              # $host would drop a non-default port
    proxy_set_header X-Forwarded-Host $http_host;
    proxy_set_header X-Forwarded-Proto $scheme;
    # SSE: don't buffer the stream, and allow long-lived connections
    proxy_buffering off;
    proxy_read_timeout 1h;
}
```

`--root-path` is what scopes the session cookie to `/celery-manager`. State-changing
requests carry a CSRF `Origin` check against the request's host: make sure the
proxy passes the original public `host:port` in `Host` and/or `X-Forwarded-Host`
(as above). Use `$http_host`, not `$host`: nginx's `$host` drops the port, so on
a public URL such as `https://ops.example.com:8443` the browser's `Origin` would
no longer match.

!!! warning "Troubleshooting: login returns 403 `cross-origin request refused`"
    The proxy is rewriting `Host` (to the upstream address, or to a name
    without the port). Make `Host` or `X-Forwarded-Host` carry the public
    `host:port` the browser used, as in the config above.
