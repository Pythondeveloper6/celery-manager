# celery-manager FastAPI demo

A small FastAPI app (`main.py`) that hosts the celery-manager dashboard under
`/celery-manager` with a plain `app.mount(...)`. The app has its own routes
(`/`, `POST /welcome/{email}`, `POST /import/{rows}`) which are untouched by
the mount. Tasks (`tasks.py`) mirror the Django demo: `send_welcome_email`,
`import_customers` (live progress bar) and `send_report` (beat schedule, fails
~30% of the time).

The key lines:

```python
dashboard, _ = create_app(CeleryManagerConfig(celery_app=celery_app, db_url=..., secret_key=...))
app.mount("/celery-manager", dashboard)
```

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# A private random key signs the dashboard's session cookies. create_app()
# refuses to start without one. (Only the web process and create_user.py need it.)
export CELERY_MANAGER_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"

python create_user.py admin 'a-strong-password'     # once
```

Then, in separate terminals (each in this directory):

```bash
redis-server
celery -A celery_app worker -l info                  # runs the tasks
celery -A celery_app beat -l info                    # fires send_report every 2 hours
celery-manager events --celery-app celery_app:app --db-url sqlite:///celery_manager_demo.db
uvicorn main:app --port 8000                         # your app + the dashboard
```

Open <http://localhost:8000/celery-manager/> and sign in. Trigger work from the dashboard (**Tasks** -> **Start task**) or with:

```bash
curl -X POST localhost:8000/welcome/me@example.com
curl -X POST localhost:8000/import/60          # watch the progress bar
```

Notes:

- `CELERY_MANAGER_COOKIE_SECURE` defaults to `0` in this demo because it is
  served over plain HTTP; the production default (`cookie_secure=True`) needs
  HTTPS. Set it to `1` behind TLS.
- Every process must use the same `CELERY_MANAGER_DB_URL` (default
  `sqlite:///celery_manager_demo.db`, relative to the working directory).
- No Redis? `CELERY_MANAGER_EAGER=1` switches to an in-memory broker and runs
  tasks inline in the web process (no worker, beat or listener needed):
  `CELERY_MANAGER_EAGER=1 uvicorn main:app`.
- Visiting `/celery-manager` (no trailing slash) redirects to
  `/celery-manager/`. The dashboard's URLs are relative, so the slash matters.
- The mounted dashboard has no lifespan/startup hooks to worry about:
  `create_app()` does all its setup eagerly, before the host starts.
