# celery-manager Flask demo

A small Flask app (`app.py`) that hosts the celery-manager dashboard under
`/celery-manager`. Flask is WSGI and the dashboard is ASGI, so the dashboard
is adapted with [`a2wsgi`](https://github.com/abersheeran/a2wsgi) and routed
with werkzeug's `DispatcherMiddleware`:

```python
dashboard, _ = create_app(CeleryManagerConfig(celery_app=celery_app, db_url=..., secret_key=...))
app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {"/celery-manager": ASGIMiddleware(dashboard)})
```

Tasks (`tasks.py`) mirror the Django demo: `send_welcome_email`,
`import_customers` (live progress bar) and `send_report` (beat schedule).

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export CELERY_MANAGER_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python create_user.py admin 'a-strong-password'     # once
```

Then, in separate terminals (each in this directory):

```bash
redis-server
celery -A celery_app worker -l info
celery -A celery_app beat -l info
celery-manager events --celery-app celery_app:app --db-url sqlite:///celery_manager_demo.db
flask --app app run --port 8000
```

Open <http://localhost:8000/celery-manager/> and sign in. Trigger work from the
dashboard (**Tasks** -> **Start task**) or with
`curl -X POST localhost:8000/welcome/me@example.com` or
`curl -X POST localhost:8000/import/60`.

No Redis? `CELERY_MANAGER_EAGER=1 flask --app app run` runs tasks inline with an
in-memory broker. `CELERY_MANAGER_COOKIE_SECURE` defaults to `0` here (plain
HTTP); set it to `1` behind TLS.

## SSE (live updates) through a2wsgi: read this

`/api/stream` is a long-lived Server-Sent-Events response. We tested it through
`a2wsgi` + the werkzeug threaded server: events **are** delivered in real time
(not buffered), but:

1. **Each open stream pins one WSGI worker thread** for as long as the tab is
   open. With gunicorn sync workers a handful of open dashboards starve your app.
2. **a2wsgi never learns the browser went away**, so a closed tab's stream is
   not cancelled: its subscriber (and an ever-growing event queue) leaks until
   the process restarts. Every page reload leaks one.

So `app.py` wraps the dashboard in `without_sse()`, which answers
`/api/stream` with `204 No Content` (EventSource stops, does not retry). The
task list falls back to polling, so the dashboard works; you only lose push
updates. (Today the SSE bus is in-process anyway, so events produced by a
separate worker or listener process don't reach it.)

If you want SSE, run the dashboard as its own ASGI process and route the
prefix to it at your reverse proxy instead of mounting it in Flask. See
[docs/integrations.md](../../docs/integrations.md#flask-wsgi).
