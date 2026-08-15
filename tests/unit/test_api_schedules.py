from datetime import timedelta

from fastapi.testclient import TestClient
from celery import Celery
from celery.schedules import schedule as interval_schedule

from celery_manager.config import CeleryManagerConfig
from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user


def _client_and_session():
    celery_app = Celery("test")
    celery_app.conf.beat_schedule = {
        "demo-every-2h": {"task": "demo.echo", "schedule": interval_schedule(run_every=timedelta(hours=2))},
    }
    config = CeleryManagerConfig(celery_app=celery_app, db_url="sqlite:///:memory:", secret_key="test-secret")
    # create_app() calls sync_periodic_schedules() at startup (Task 10 Step 5),
    # which is what populates periodic_schedules from beat_schedule above.
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "alice", "pw")
    client = TestClient(app)
    client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    return client


def test_pause_occurrence_endpoint_creates_override():
    client = _client_and_session()
    target = "2026-08-15T20:00:00+00:00"
    response = client.post(
        "/api/schedules/demo-every-2h/occurrences/pause",
        json={"target_time": target, "action": "skip"},
    )
    assert response.status_code == 200
    assert response.json()["action"] == "skip"


def test_list_occurrences_endpoint_shows_computed_upcoming_runs():
    client = _client_and_session()
    response = client.get("/api/schedules/demo-every-2h/occurrences")
    assert response.status_code == 200
    body = response.json()
    assert len(body) > 0
    assert "target_time" in body[0]
    assert body[0]["action"] is None


def test_paused_occurrence_shows_as_paused_on_next_request():
    # Each TestClient request opens its own session via session_factory()
    # (see Task 10 Step 5's create_app wiring) — this deliberately checks
    # the pause takes effect across two separate requests/sessions, not
    # just within one, since that's what a real browser does and it's the
    # exact scenario Task 6's _as_aware_utc normalization exists for.
    client = _client_and_session()
    occurrences = client.get("/api/schedules/demo-every-2h/occurrences").json()
    target_time = occurrences[0]["target_time"]

    pause_response = client.post(
        "/api/schedules/demo-every-2h/occurrences/pause",
        json={"target_time": target_time, "action": "skip"},
    )
    assert pause_response.status_code == 200

    refreshed = client.get("/api/schedules/demo-every-2h/occurrences").json()
    assert refreshed[0]["action"] == "skip"
    assert refreshed[0]["consumed"] is False
