"""A small Flask app that hosts the celery-manager dashboard under
/celery-manager.

Flask is WSGI and the dashboard is ASGI, so a2wsgi's ASGIMiddleware adapts the
dashboard to WSGI and werkzeug's DispatcherMiddleware routes the prefix to it.

    flask --app app run --port 8000      # or any WSGI server: gunicorn app:app
    open http://localhost:8000/celery-manager/
"""
import settings
from a2wsgi import ASGIMiddleware
from celery_app import app as celery_app
from flask import Flask, jsonify
from tasks import import_customers, send_welcome_email
from werkzeug.middleware.dispatcher import DispatcherMiddleware

from celery_manager.api.app import create_app

dashboard, _session_factory = create_app(settings.make_config(celery_app, with_secret=True))

app = Flask(__name__)


# --- your own application ---------------------------------------------------
@app.get("/")
def index():
    return jsonify(hello="this is my app", dashboard="/celery-manager/")


@app.post("/welcome/<email>")
def welcome(email):
    return jsonify(task_id=send_welcome_email.delay(email).id)


@app.post("/import/<int:rows>")
def import_rows(rows):
    return jsonify(task_id=import_customers.delay(rows).id)


# --- the dashboard ----------------------------------------------------------
def without_sse(asgi_app):
    """Answer GET /api/stream with 204 No Content (browsers' EventSource treats
    that as "stop, don't reconnect"). Through a2wsgi each open stream pins a
    WSGI worker thread, and a closed browser tab is never noticed: the stream's
    subscriber and its queue leak until the process restarts. The dashboard
    falls back to polling, so nothing is lost. See the README, "SSE".
    Drop this wrapper if you serve the dashboard from its own uvicorn process."""

    async def wrapper(scope, receive, send):
        if scope["type"] == "http" and scope["path"].rstrip("/").endswith("/api/stream"):
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        await asgi_app(scope, receive, send)

    return wrapper


# DispatcherMiddleware moves the prefix into SCRIPT_NAME, which a2wsgi passes on
# as the ASGI root_path -- that is what scopes the session cookie to
# /celery-manager. Wrapping `app.wsgi_app` (not `app`) is the standard Flask
# idiom: `app` stays a real Flask object for `flask --app app` and extensions.
app.wsgi_app = DispatcherMiddleware(
    app.wsgi_app,
    {"/celery-manager": ASGIMiddleware(without_sse(dashboard))},
)
