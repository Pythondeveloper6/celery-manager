from datetime import datetime, timedelta, timezone
from unittest.mock import patch

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
    client = TestClient(app, base_url="https://testserver")
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


def test_pause_occurrence_endpoint_writes_an_audit_row():
    """I2: pausing/cancelling a future occurrence is the product's headline
    operator action and had no audit trail at all — only retry did."""
    client = _client_and_session()
    response = client.post(
        "/api/schedules/demo-every-2h/occurrences/pause",
        json={"target_time": "2026-08-15T20:00:00+00:00", "action": "skip"},
    )
    assert response.status_code == 200

    rows = [r for r in client.get("/api/audit").json() if r["action"] == "pause_occurrence"]
    assert len(rows) == 1
    assert rows[0]["username"] == "alice"
    assert rows[0]["target_type"] == "schedule"
    assert rows[0]["target_id"] == "demo-every-2h"
    assert "2026-08-15T20:00:00" in rows[0]["details"]
    assert "skip" in rows[0]["details"]


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
    # just within one, since that's what a real browser does.
    #
    # Between the first GET and the second GET we advance the clock by 5
    # real seconds (well under the default 60s eta_tolerance_seconds, but
    # enough that the two independently-recomputed "next occurrence"
    # timestamps are NOT byte-identical) — the exact realistic gap
    # (dashboard loads, user pauses a few seconds later, dashboard
    # refreshes) that motivated switching list_upcoming_with_overrides
    # from exact datetime equality to tolerance-window matching.
    client = _client_and_session()

    first_now = datetime(2026, 8, 15, 6, 0, 0, tzinfo=timezone.utc)
    pause_now = datetime(2026, 8, 15, 6, 0, 2, tzinfo=timezone.utc)
    second_now = datetime(2026, 8, 15, 6, 0, 5, tzinfo=timezone.utc)

    with patch("celery_manager.control.schedules.datetime") as mock_dt:
        mock_dt.now.side_effect = [first_now, pause_now, second_now]
        occurrences = client.get("/api/schedules/demo-every-2h/occurrences").json()
        first_target_time = occurrences[0]["target_time"]

        pause_response = client.post(
            "/api/schedules/demo-every-2h/occurrences/pause",
            json={"target_time": first_target_time, "action": "skip"},
        )
        assert pause_response.status_code == 200

        refreshed = client.get("/api/schedules/demo-every-2h/occurrences").json()

    # The two GETs really did compute different timestamps for "the same"
    # occurrence — proving this test isn't accidentally passing because
    # both requests happened to land on an identical from_time.
    assert refreshed[0]["target_time"] != first_target_time
    assert refreshed[0]["action"] == "skip"
    assert refreshed[0]["consumed"] is False


def test_list_schedules_endpoint_returns_synced_schedules():
    client = _client_and_session()
    response = client.get("/api/schedules")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["name"] == "demo-every-2h"
    assert body[0]["task_name"] == "demo.echo"
    assert body[0]["enabled"] is True


def test_list_schedules_endpoint_requires_auth():
    from celery import Celery
    from celery_manager.api.app import create_app
    from celery_manager.config import CeleryManagerConfig
    from fastapi.testclient import TestClient

    config = CeleryManagerConfig(celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret")
    app, _ = create_app(config)
    response = TestClient(app, base_url="https://testserver").get("/api/schedules")
    assert response.status_code == 401


def test_occurrences_endpoint_handles_a_bare_number_beat_schedule_entry():
    """I3 at the HTTP boundary: a bare-number `schedule` used to raise
    AttributeError out of the route, i.e. a 500."""
    celery_app = Celery("test")
    celery_app.conf.beat_schedule = {"bare-every-30s": {"task": "demo.echo", "schedule": 30.0}}
    config = CeleryManagerConfig(celery_app=celery_app, db_url="sqlite:///:memory:", secret_key="test-secret")
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "alice", "pw")
    client = TestClient(app, base_url="https://testserver")
    client.post("/api/auth/login", json={"username": "alice", "password": "pw"})

    response = client.get("/api/schedules/bare-every-30s/occurrences")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 10
    assert body[0]["action"] is None


def test_pause_rejects_reschedule_action_with_422():
    """I6: `action` used to be a bare `str`, so "reschedule" was accepted,
    stored, and displayed in the dashboard as a paused occurrence — while the
    decorator only ever acts on skip/cancel, so it did nothing at all."""
    client = _client_and_session()

    response = client.post(
        "/api/schedules/demo-every-2h/occurrences/pause",
        json={"target_time": "2026-08-15T20:00:00+00:00", "action": "reschedule"},
    )

    assert response.status_code == 422
    # And nothing was persisted for it.
    assert all(o["action"] is None for o in client.get("/api/schedules/demo-every-2h/occurrences").json())


def test_pause_rejects_an_arbitrary_action_with_422():
    client = _client_and_session()
    response = client.post(
        "/api/schedules/demo-every-2h/occurrences/pause",
        json={"target_time": "2026-08-15T20:00:00+00:00", "action": "delete-everything"},
    )
    assert response.status_code == 422


def test_pause_still_accepts_skip_and_cancel():
    client = _client_and_session()
    for action in ("skip", "cancel"):
        response = client.post(
            "/api/schedules/demo-every-2h/occurrences/pause",
            json={"target_time": "2026-08-15T20:00:00+00:00", "action": action},
        )
        assert response.status_code == 200, action
        assert response.json()["action"] == action
