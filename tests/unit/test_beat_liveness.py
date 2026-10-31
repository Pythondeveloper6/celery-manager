"""Beat liveness must never dispatch work or block on a monitor request."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from celery import Celery
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from celery_manager.beat_health import (
    OWNER_PREFIX,
    _maybe_ping,
    _NoRedirect,
    _send_ping,
    owner_key,
    record_tick,
)
from celery_manager.control.health import beat_health
from celery_manager.db.models import AppSetting, Base


def scheduler():
    app = Celery("liveness", broker="memory://")
    return SimpleNamespace(app=app)


def test_external_ping_is_throttled_single_flight_and_independent_of_db():
    beat = scheduler()
    beat.app.conf.update(cm_beat_healthcheck_url="https://monitor.test/secret", cm_beat_healthcheck_timeout=999)
    thread = MagicMock()
    thread.is_alive.return_value = True
    with patch("celery_manager.beat_health.threading.Thread", return_value=thread) as factory:
        assert _maybe_ping(beat, 0) == 30
        _maybe_ping(beat, 31)
        assert factory.call_count == 1
        assert factory.call_args.kwargs["daemon"] is True
        assert factory.call_args.kwargs["args"][1] == 10
        thread.is_alive.return_value = False
        _maybe_ping(beat, 32)
        assert factory.call_count == 2
    with patch("celery_manager.beat_health._maybe_ping", return_value=15):
        assert record_tick(beat) == 15


def test_external_ping_does_not_follow_redirects_or_log_token(caplog):
    response = MagicMock(status=204)
    response.__enter__.return_value = response
    opener = MagicMock()
    opener.open.return_value = response
    with patch("celery_manager.beat_health.build_opener", return_value=opener) as build:
        _send_ping("https://monitor.test/very-secret-token", 1)
        assert isinstance(build.call_args.args[0], _NoRedirect)
        assert opener.open.call_args.kwargs["timeout"] == 1
        opener.open.side_effect = OSError("https://monitor.test/very-secret-token")
        _send_ping("https://monitor.test/very-secret-token", 1)
    assert "heartbeat failed" in caplog.text and "very-secret-token" not in caplog.text
    assert _NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.test") is None


def test_bad_monitor_configuration_and_thread_start_do_not_break_tick(caplog):
    with patch("celery_manager.beat_health.build_opener") as build:
        _send_ping("file:///secret", 1)
        _send_ping("https://user:secret@monitor.test", 1)
        build.assert_not_called()
    beat = scheduler()
    beat.app.conf.cm_beat_healthcheck_url = "https://monitor.test/ping"
    with patch("celery_manager.beat_health.threading.Thread", side_effect=RuntimeError("no threads")):
        assert record_tick(beat) == 30
    assert "could not start" in caplog.text


def test_durable_owners_cannot_mask_each_other_and_legacy_shape_remains():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    beat = scheduler()
    beat.app.conf.update(celery_manager_db_url="configured", cm_beat_owner_id="orders")
    beat._cm_health_sessions = sessions
    with patch("celery_manager.beat_health.time.monotonic", return_value=0):
        assert record_tick(beat) == 30
    with sessions() as session:
        assert session.get(AppSetting, "beat_last_tick") is not None
        key = owner_key("orders")
        data = json.loads(session.get(AppSetting, key).value)
        assert data["owner_id"] == "orders"
        now = datetime.fromisoformat(data["at"])
        row = AppSetting(key=owner_key("billing"),
                         value=json.dumps({"at": (now - timedelta(seconds=121)).isoformat(), "owner_id": "billing"}),
                         updated_at=now)
        session.add(row)
        session.commit()
        result = beat_health(session, now=now)
        assert result["state"] == "stale" and result["age_seconds"] == 0
        assert len(result["owners"]) == 2
        assert beat_health(session, now=now, owner_id="orders")["state"] == "healthy"
        assert beat_health(session, now=now, owner_id="missing")["state"] == "unknown"
        assert beat_health(session, now=now, stale_seconds=121, owner_id="billing")["state"] == "healthy"
        assert beat_health(session, now=now, stale_seconds=120, owner_id="billing")["state"] == "stale"
    engine.dispose()


def test_malformed_owner_heartbeat_is_unknown():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        session.add(AppSetting(key=OWNER_PREFIX + "bad", value="[]", updated_at=datetime.now(timezone.utc)))
        session.commit()
        assert beat_health(session)["state"] == "unknown"
    engine.dispose()


def test_beat_owner_retirement_is_admin_only_and_audited():
    from fastapi.testclient import TestClient

    from celery_manager import CeleryManagerConfig
    from celery_manager.api.app import create_app
    from celery_manager.auth.external import ExternalUser
    from celery_manager.db.models import AuditLog
    app = Celery("retirement", broker="memory://")
    config = CeleryManagerConfig(celery_app=app, db_url="sqlite:///:memory:", secret_key="s",
                                authenticate_request=lambda r: ExternalUser("host", r.headers.get("x-role", "admin")))
    api, sessions = create_app(config)
    beat = SimpleNamespace(app=app, _cm_health_sessions=sessions)
    app.conf.update(celery_manager_db_url="configured", cm_beat_owner_id="old-deployment")
    record_tick(beat)
    client = TestClient(api, base_url="https://testserver")
    path = "/api/health/beat/owner?owner_id=old-deployment"
    assert client.delete(path, headers={"x-role": "viewer"}).status_code == 403
    assert client.get("/api/health/beat?owner_id=old-deployment").json()["state"] == "healthy"
    assert client.delete(path).json()["removed"] is True
    assert client.get("/api/health/beat").json()["state"] == "unknown"
    with sessions() as session:
        assert session.query(AuditLog).filter_by(action="beat.retire_owner").count() == 1


def test_owner_key_fits_every_supported_database_column():
    assert len(owner_key("a" * 500)) <= AppSetting.__table__.c.key.type.length
    assert owner_key("orders") != owner_key("billing")
