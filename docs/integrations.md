# Integrating with your app

`create_app(config)` returns a self-contained ASGI application (a FastAPI app)
that serves the dashboard at `/`, the REST API at `/api/*` and the live-update
stream at `/api/stream`. You can run it on its own, or **mount it under a
prefix** (say `/celery-manager`) inside the web app you already have. The
dashboard's URLs are all relative, so the same build works at the root and under
any prefix.

Runnable projects for each framework live in the repository:
[`examples/fastapi_demo`](https://github.com/Pythondeveloper6/celery-manager/tree/main/examples/fastapi_demo),
[`examples/flask_demo`](https://github.com/Pythondeveloper6/celery-manager/tree/main/examples/flask_demo) and
[`examples/django_demo`](https://github.com/Pythondeveloper6/celery-manager/tree/main/examples/django_demo).

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

!!! warning "Use polling through WSGI/a2wsgi"
    Set `config.live_stream = "poll"` before `create_app(config)`. The API omits
    `/api/stream` and the SPA uses polling without repeatedly opening EventSource
    connections. Long-lived SSE connections consume WSGI workers and adapters may
    not promptly notice disconnects. Keep `"sse"` for an ASGI deployment that
    supports streaming. Older demos' `without_sse` wrapper is no longer necessary
    when this option is set.

For OIDC, bridge sessions, trusted origins, frame ancestors, immediate task links
and external schedule adapters, see the [production adoption guide](production.md).

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
| Beat | `celery -A yourapp beat -S celery_manager.beat:Scheduler` | Only if you have periodic tasks. The scheduler stamps each firing so a skip applies to exactly that run; see [How the skip/cancel check works](decorator.md#how-the-skipcancel-check-works). |
| Events listener | `celery-manager events --celery-app yourapp:app --db-url sqlite:///celery_manager.db` | Records every task, including undecorated ones, and the queued/started transitions. |

```python
# tasks.py (imported by the worker)
import celery_manager
from celery_manager.config import CeleryManagerConfig

celery_manager.setup(CeleryManagerConfig(celery_app=app, db_url=DB_URL))
```

The events listener needs Celery task events switched on; see
[Required Celery settings](quickstart.md#required-celery-settings).

Create a dashboard user with `celery-manager create-user NAME --role admin --db-url ...`
or `celery_manager.auth.users.create_user`; both demos include a `create_user.py`
script. Or skip separate accounts altogether and let people sign in with your app's
own users: see the next section.

## Sign in with your app's users

Instead of keeping a second set of accounts, the dashboard can ask your Django,
Flask or FastAPI app who a request belongs to, and which [role](roles.md) they get.
Set `authenticate_request` on the config to a function that takes the Starlette
`Request` and returns an `ExternalUser(username, role)`, or `None` when the request
isn't signed in to your app:

```python
from celery_manager.integrations import ExternalUser

def authenticate(request) -> ExternalUser | None:
    user = my_app_user_from(request)          # your cookie or token, your user table
    if user is None:
        return None                           # not signed in to my app
    return ExternalUser(username=user.email, role=role_for(user))

config = CeleryManagerConfig(
    ...,
    authenticate_request=authenticate,
    login_url="/login?next={next}",           # where "Sign in with ..." goes
    logout_url="/logout",                     # where "Sign out" goes
    login_provider_name="Acme",               # "Sign in with Acme"
    local_login_enabled=False,                # optional: no dashboard passwords at all
)
```

Ready-made authenticators for Django and Flask are below. For FastAPI you write the
function yourself, usually a few lines around the code you already use to check a
user.

### How a request is signed in

On **every** request, in this order:

1. **Your app is asked first.** If it returns an `ExternalUser`, that is who the
   request belongs to, even if the browser also has an older dashboard session for a
   local account.
    - `role` is `"viewer"`, `"operator"` or `"admin"`: signed in with that role.
    - `role` is `None`: a real user of your app who may not use the dashboard. Every
      request gets `403` and the dashboard shows a "No access" page naming the
      account. There is no fallback to a local account in the same browser.
2. **It returned `None`** (not signed in to your app): the dashboard's own accounts
   and `cm_session` cookie are tried, as before. With `local_login_enabled=False`
   that step is skipped and the request is a `401`.

Because the role comes from your app on each request, demoting someone, removing
their access or deactivating them in your app applies on their next request.

**It fails closed.** A role that isn't one of the three (a typo such as `"Admin"`,
a role from your own app such as `"superuser"`), anything that isn't an
`ExternalUser`, an empty or over-long username, or an exception from your function
is a `500` with the reason in the server log ("authenticate_request returned role
'superuser' for 'dana' ..."). It is never turned into access, and never into "not
signed in", which would hide the bug behind the login page. An `HTTPException`
raised by your function (say, a dependency that raises its own `401`) is passed
through unchanged.

Your function runs in FastAPI's worker threadpool, so it may block: a database query
or an ORM call is fine. An `async def` function works too. It is called once per
request, so keep it as cheap as your app's own per-request user lookup.

The dashboard reports where the account came from: `GET /api/whoami` returns
`"source": "external"` for your app's users and `"local"` for its own accounts.

### Django

```python
from celery_manager.integrations.django import django_authenticator

config = CeleryManagerConfig(
    celery_app=celery_app,
    db_url=settings.CELERY_MANAGER_DB_URL,
    secret_key=settings.CELERY_MANAGER_SECRET_KEY,
    authenticate_request=django_authenticator(),
    login_url="/admin/login/?next={next}",
    login_provider_name="the Django admin",
)
```

It reads Django's session cookie (`SESSION_COOKIE_NAME`, `sessionid` by default),
loads the session with your `SESSION_ENGINE` and the user with the backend that
signed them in, and checks the session's auth hash the way `django.contrib.auth`
does, so changing a password in Django ends their dashboard access too. Inactive
users get no access. It only ever reads the session: it never re-keys or flushes it,
since a changed session cookie can't be delivered from the dashboard's response and
would sign the person out of your Django site.

Requirements:

- **Django must be set up in the dashboard's process** with the same settings
  (database, `SECRET_KEY`, session engine): mount the dashboard in your ASGI
  application, or call `django.setup()` before `create_app()` the way
  `examples/django_demo/run_dashboard.py` does.
- **The browser must send Django's cookie to the dashboard**, which it does when
  both are served under the same host name: mounted in the same app, or behind one
  reverse proxy. Cookies don't depend on the port, so `localhost:8000` (Django) and
  `localhost:8001` (the dashboard) share them in development.

**Roles.** By default superusers are `admin`, other staff (`is_staff`) are `operator`,
and everyone else has no access. To use permissions instead, define them on any
model of yours and use `permission_roles`; each user gets the highest role they hold
a permission for, directly or through a group:

```python
from celery_manager.integrations.django import django_authenticator, permission_roles

class OpsSettings(models.Model):          # any model works; this one can be empty
    class Meta:
        permissions = [
            ("celery_manager_admin", "Can administer celery-manager"),
            ("celery_manager_operate", "Can operate celery-manager"),
            ("celery_manager_view", "Can view celery-manager"),
        ]

authenticate_request = django_authenticator(
    role_for=permission_roles(
        admin="ops.celery_manager_admin",
        operator="ops.celery_manager_operate",
        viewer="ops.celery_manager_view",
    ),
)
```

Active superusers hold every permission, so they are always admins under this
mapping. Any other rule is a function of the user, for example by group:

```python
def role_for(user):
    groups = set(user.groups.values_list("name", flat=True))
    if "SRE" in groups:
        return "admin"
    if "Support" in groups:
        return "viewer"
    return None

authenticate_request = django_authenticator(role_for=role_for, username_for=lambda u: u.email)
```

**Signing out.** Django's `LogoutView` accepts only `POST` (since Django 5.0), and the
dashboard's sign-out is a plain link, so point `logout_url` at a page of yours that
has a sign-out button, or at a small view that logs out on `GET`. Without a
`logout_url`, people signed in through Django get no sign-out item in the dashboard.

### Flask

For the dashboard mounted in your Flask process (a2wsgi + `DispatcherMiddleware`, as
in [Flask (WSGI)](#flask-wsgi)), where the browser sends Flask's session cookie with
every dashboard request:

```python
from celery_manager.integrations.flask import flask_authenticator

app = Flask(__name__)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

dashboard, _ = create_app(CeleryManagerConfig(
    ...,
    authenticate_request=flask_authenticator(
        app,
        load_user=load_user,
        role_for=lambda user: {"sre": "admin", "dev": "operator"}.get(user.team),
    ),
    login_url="/login?next={next}",
    logout_url="/logout",
))
app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {"/celery-manager": ASGIMiddleware(dashboard)})
```

It opens Flask's session through your app's own session interface (so the cookie's
signature is checked with your `SECRET_KEY`, and a server-side session extension
works the same way), takes the user id from `session["_user_id"]` (where Flask-Login
keeps it; change it with `user_id_key=`), and calls your `load_user` inside a request
context of your app, so `db.session` and other extensions work there. `role_for` is
required, since Flask has no built-in notion of staff. The username shown and
audited is the user's `username`, else `email`, else `get_id()`; pass
`username_for=` to choose. Users with `is_active = False` get no access.

Flask-Login's "remember me" cookie is not used: restoring a session from it means
writing a new session cookie, which only your app's response can do. Someone signed
in only by that cookie is sent to `login_url` and comes back with a session.

Flask doesn't set `SameSite` on its session cookie unless you do. The helper logs a
warning if `SESSION_COOKIE_SAMESITE` is unset; see [Security notes](#security-notes)
for why the dashboard is still protected and why you should set it anyway.

### FastAPI and Starlette

There is no single way FastAPI apps sign people in, so you write the function. Reuse
whatever your own routes use to find the user:

```python
from celery_manager.integrations import ExternalUser

def authenticate(request):
    token = request.cookies.get("access_token")
    if not token:
        return None
    claims = verify_jwt(token)                 # your own verification
    if claims is None:
        return None
    role = {"platform": "admin", "oncall": "operator"}.get(claims.get("team"))
    return ExternalUser(username=claims["sub"], role=role)

dashboard, _ = create_app(CeleryManagerConfig(
    ...,
    authenticate_request=authenticate,
    login_url="/login?next={next}",
))
app.mount("/celery-manager", dashboard)
```

An `async def` function is fine, for an async database or token service. Reading an
`Authorization: Bearer ...` header works too, for scripts calling the
[REST API](rest-api.md); a bearer header isn't sent by the browser on its own, so it
can't be used for cross-site request forgery.

### Sign-in screen and sign-out

| Option | Default | What it does |
|---|---|---|
| `login_url` | `None` | Where "Sign in with ..." links to. `{next}` is replaced with the dashboard's own address (URL-encoded), so your login page can send people back. A path (`/accounts/login/`) or an `http(s)` URL; anything else (`javascript:`, `//other-host`) makes `create_app()` refuse to start. Without it, the login screen asks people to sign in to your app and come back. |
| `logout_url` | `None` | Where "Sign out" goes for people signed in through your app (a plain link, so a `GET`). The dashboard clears its own cookie first. Without it they get no sign-out item: clearing only the dashboard's cookie would not sign them out of your app. |
| `login_provider_name` | `"your app"` | What the screens call your app: "Sign in with Acme". |
| `local_login_enabled` | `True` | `False` makes your app the only way in: the password form is replaced by the "Sign in with ..." link, `POST /api/auth/login` is refused with `403`, and existing dashboard sessions stop working. `create_app()` refuses `False` without an `authenticate_request`. |

With local login on, the login screen shows the password form and, if `login_url` is
set, a "Sign in with ..." link under it. Keeping one local admin account is a good
break-glass for when your app's sign-in is down.

People signed in through your app have no password here: the dashboard hides
**Change password** for them and `POST /api/me/password` answers `409`. The
[Users page](roles.md#managing-accounts) still manages local accounts and is titled
**Local accounts** while an authenticator is configured. Your app's users are not
listed there, because their roles live in your app.

### Security notes

- **Cross-site request forgery is still blocked.** The dashboard's CSRF defence is the
  `Origin` check on every `POST`/`PUT`/`PATCH`/`DELETE` (see
  [REST API: auth](rest-api.md#auth)). It runs before any sign-in check and doesn't
  depend on which cookie would have signed the request in, so a request from another
  site, or from a sibling subdomain, is refused with `403` before your authenticator
  is even called. That matters because your app's cookie may be weaker than the
  dashboard's own: `cm_session` is always `SameSite=Lax`, Django's `sessionid` is `Lax`
  by default, but Flask's session cookie has no `SameSite` unless you set
  `SESSION_COOKIE_SAMESITE`. Every current browser sends `Origin` on cross-origin
  writes, so the check holds regardless; set `SameSite=Lax` on your own cookie anyway,
  as the second layer `cm_session` has. Your app's own CSRF middleware (Django's
  `CsrfViewMiddleware`, Flask-WTF) does not cover the mounted dashboard, and doesn't
  need to.
- **Your app's session is now a dashboard credential.** Anything that can steal or
  ride your app's session (an XSS hole anywhere on the same host) can use the
  dashboard with that user's role. Give dashboard roles to the accounts you would trust
  with them, not to every signed-in customer: the default Django mapping admits staff
  only, and `None` is always "no access".
- **The audit log records the username your function returns**, as is, the same as
  for a local account. Choose a username people recognise (an email address is
  unambiguous). If you keep local accounts too, don't give them names your app's
  users have, or the audit log can't tell the two apart.
- **The dashboard never writes to your app's session.** The helpers only read it, so
  signing in to the dashboard can't sign anyone out of your app or extend their
  session.

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
