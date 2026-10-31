# Is celery-manager the right tool?

## Decision guide

| You want | Use |
|---|---|
| A live view of workers and tasks, nothing stored, no accounts | **Flower**. celery-manager can replace it, but Flower is lighter for that alone. |
| Durable run history, failure inbox, retry from a UI, audit log, roles, SSO | **celery-manager** |
| Pause or skip *one* future occurrence of a periodic task without editing beat | **celery-manager** (no other Celery tool does this) |
| Edit beat schedules from a UI | django-celery-beat admin, RedBeat tooling, or celery-manager (which wraps those backends and adds audit, preview and pause) |
| Know when a scheduled job did not run, without self-hosting | **Sentry Crons / Healthchecks.io / Cronitor**. celery-manager detects missed runs and beat outages itself, and can also ping one of those services as an [external dead-man's switch](beat-liveness.md). |
| Graphs, long-term metrics, paging | **Prometheus + Grafana** fed by celery-manager's [`/metrics`](metrics.md) and the shipped [dashboard and alert rules](metrics.md#grafana-dashboard-and-alert-rules). |
| Orchestrate DAGs, data pipelines, long workflows | **Airflow, Prefect, Temporal**. Different category. |
| Job history for arq, Dramatiq, Taskiq or a hand-written loop | celery-manager's [other-runner adapters](other-runners.md) (history, failures and alerts; Celery-only controls do not apply) |

celery-manager is a poor fit when you do not run Celery and only need a cron
log (a small table in your own app is cheaper), or when Flower plus Grafana
already answers every question your team asks.

## Quickstarts

Each recipe assumes `celery_app` is your Celery instance and `SECRET` a
private random string. Run `celery-manager create-user admin --role admin
--db-url sqlite:///cm.db` once to get a login. Workers call
`celery_manager.setup(config)` with the same `db_url` (see
[Integrating with your app](integrations.md#the-worker-beat-and-events-listener)).

### FastAPI

```python
from fastapi import FastAPI
from celery_manager import CeleryManagerConfig
from celery_manager.api.app import create_app

from myproject.celery import celery_app

config = CeleryManagerConfig(celery_app=celery_app, db_url="sqlite:///cm.db", secret_key=SECRET)
dashboard, _ = create_app(config)
app = FastAPI()
app.mount("/celery-manager", dashboard)   # uvicorn main:app
```

### Flask

```python
from a2wsgi import ASGIMiddleware            # pip install "celery-manager[flask]"
from flask import Flask
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from celery_manager import CeleryManagerConfig
from celery_manager.api.app import create_app

config = CeleryManagerConfig(celery_app=celery_app, db_url="sqlite:///cm.db", secret_key=SECRET, live_stream="poll")
dashboard, _ = create_app(config)
app = Flask(__name__)
app.wsgi_app = DispatcherMiddleware(app.wsgi_app, {"/celery-manager": ASGIMiddleware(dashboard)})
```

### Django (separate process)

```python
# dashboard.py - run with: uvicorn dashboard:app --port 8001
import django, os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mysite.settings")
django.setup()
from celery_manager import CeleryManagerConfig
from celery_manager.api.app import create_app
from celery_manager.integrations.django import django_authenticator
from mysite.celery import app as celery_app

config = CeleryManagerConfig(celery_app=celery_app, db_url="sqlite:///cm.db", secret_key=SECRET,
                             authenticate_request=django_authenticator())
app, _ = create_app(config)
```

### Observe-only (no Celery)

```python
from celery_manager import CeleryManagerConfig, RunObserver

observer = RunObserver(CeleryManagerConfig(celery_app=None, db_url="sqlite:///cm.db"))

@observer.observe("reports.nightly")
async def nightly_report():
    ...
```

### Try it without any of that

```shell
pip install "celery-manager[standalone]"
celery-manager demo          # SQLite + seeded runs, opens http://127.0.0.1:8765/
```
