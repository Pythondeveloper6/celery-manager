"""A small FastAPI app that hosts the celery-manager dashboard under
/celery-manager.

    uvicorn main:app --port 8000
    open http://localhost:8000/celery-manager/
"""
import settings
from celery_app import app as celery_app
from fastapi import FastAPI
from tasks import import_customers, send_welcome_email

from celery_manager.api.app import create_app

# The dashboard. create_app() returns (fastapi_app, session_factory); the
# factory is only needed if you want direct DB access.
dashboard, _session_factory = create_app(settings.make_config(celery_app, with_secret=True))

app = FastAPI(title="My app")


# --- your own application ---------------------------------------------------
@app.get("/")
def index():
    return {"hello": "this is my app", "dashboard": "/celery-manager/"}


@app.post("/welcome/{email}")
def welcome(email: str):
    result = send_welcome_email.delay(email)
    return {"task_id": result.id}


@app.post("/import/{rows}")
def import_rows(rows: int):
    return {"task_id": import_customers.delay(rows).id}


# --- the dashboard ----------------------------------------------------------
# Mount LAST (or at least not at "/"): a mount only claims its own prefix, so
# your routes above are untouched. Open it WITH the trailing slash; the bare
# /celery-manager is redirected to /celery-manager/ for you.
app.mount("/celery-manager", dashboard)
