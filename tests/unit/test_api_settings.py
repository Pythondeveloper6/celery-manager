import pytest
from celery import Celery
from fastapi.testclient import TestClient

from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user
from celery_manager.config import CeleryManagerConfig
from celery_manager.control.settings import (
    DEFAULT_SETTINGS,
    get_settings,
    update_settings,
)
from celery_manager.db.models import AppSetting, AuditLog


def _app_and_session():
    """An app with one user seeded and nothing in app_settings."""
    config = CeleryManagerConfig(celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret")
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "alice", "pw")
        session.commit()
    return app, session_factory


def _client_and_session():
    """A logged-in client (session cookie set) plus the session factory."""
    app, session_factory = _app_and_session()
    client = TestClient(app, base_url="https://testserver")
    client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    return client, session_factory


# --- GET: defaults and the pre-auth requirement ---------------------------


def test_get_settings_returns_defaults_when_nothing_is_stored():
    client, _ = _client_and_session()
    response = client.get("/api/settings")
    assert response.status_code == 200
    assert response.json() == {
        "brand_name": "celery-manager",
        "brand_accent": "",
        "brand_logo_url": "",
    }


def test_get_settings_works_without_a_session_cookie():
    """Pins the deliberate design decision: this route is unauthenticated.

    The login screen renders the deployment's branding and is by definition
    pre-auth, so a 401 here would mean stock branding on the one screen most
    users see. If someone ever adds `Depends(get_current_user)` to this route,
    this test is the thing that tells them the login page just lost its logo.
    """
    app, _ = _app_and_session()
    response = TestClient(app, base_url="https://testserver").get("/api/settings")
    assert response.status_code == 200
    assert response.json()["brand_name"] == "celery-manager"


def test_get_settings_never_leaks_who_changed_it():
    """`updated_by` is a real username; an unauthenticated endpoint must not
    hand one out. The response shape is exactly DEFAULT_SETTINGS' keys."""
    client, _ = _client_and_session()
    client.put("/api/settings", json={"brand_name": "Acme Jobs"})

    body = TestClient(client.app, base_url="https://testserver").get("/api/settings").json()

    assert set(body) == set(DEFAULT_SETTINGS)
    assert "updated_by" not in body
    assert "updated_at" not in body


def test_get_settings_reflects_stored_values():
    client, session_factory = _client_and_session()
    with session_factory() as session:
        session.add(AppSetting(key="brand_name", value="Acme Jobs"))
        session.commit()

    body = client.get("/api/settings").json()
    assert body["brand_name"] == "Acme Jobs"
    # Unset keys still come back, at their defaults — callers never handle a
    # missing key.
    assert body["brand_accent"] == ""
    assert body["brand_logo_url"] == ""


# --- PUT: auth, partial updates -------------------------------------------


def test_put_settings_requires_auth():
    app, _ = _app_and_session()
    response = TestClient(app, base_url="https://testserver").put(
        "/api/settings", json={"brand_name": "Acme Jobs"}
    )
    assert response.status_code == 401


def test_put_updates_a_subset_and_leaves_the_others_alone():
    client, _ = _client_and_session()
    client.put("/api/settings", json={"brand_name": "Acme Jobs", "brand_accent": "#00ffcc"})

    response = client.put("/api/settings", json={"brand_accent": "#ff0"})

    assert response.status_code == 200
    assert response.json() == {
        "brand_name": "Acme Jobs",
        "brand_accent": "#ff0",
        "brand_logo_url": "",
    }
    assert client.get("/api/settings").json()["brand_name"] == "Acme Jobs"


def test_put_records_who_changed_it():
    client, session_factory = _client_and_session()
    client.put("/api/settings", json={"brand_name": "Acme Jobs"})

    with session_factory() as session:
        row = session.query(AppSetting).filter_by(key="brand_name").one()
        assert row.value == "Acme Jobs"
        assert row.updated_by == "alice"
        assert row.updated_at is not None


def test_put_rejects_unknown_keys():
    """A typo must not return 200 with nothing stored."""
    client, session_factory = _client_and_session()
    response = client.put("/api/settings", json={"brand_colour": "#00ffcc"})

    assert response.status_code == 422
    with session_factory() as session:
        assert session.query(AppSetting).count() == 0


# --- Validation: the security-relevant rules ------------------------------


@pytest.mark.parametrize(
    "value",
    [
        # The CSS-injection shape: this string would land in
        # `--accent: <value>` and rewrite the page's styles.
        "red; background: url(https://evil.example/x)",
        "red",
        "rgb(255,0,0)",
        "var(--x)",
        "#00ffcc; }",
        "#12345",
        "00ffcc",
        "#ggg",
        "expression(alert(1))",
    ],
)
def test_put_rejects_invalid_brand_accent(value):
    client, session_factory = _client_and_session()
    response = client.put("/api/settings", json={"brand_accent": value})

    assert response.status_code == 422, value
    assert "brand_accent" in response.json()["detail"]
    with session_factory() as session:
        assert session.query(AppSetting).count() == 0


@pytest.mark.parametrize("value", ["#0ff", "#00ffcc", "#00FFCC", ""])
def test_put_accepts_valid_brand_accent(value):
    client, _ = _client_and_session()
    response = client.put("/api/settings", json={"brand_accent": value})
    assert response.status_code == 200, value
    assert response.json()["brand_accent"] == value


@pytest.mark.parametrize(
    "value",
    [
        # The one that matters: this lands in an <img src>, so accepting it is
        # stored XSS against every visitor of a pre-auth page.
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "  javascript:alert(1)  ",
        "vbscript:msgbox(1)",
        "file:///etc/passwd",
        # Protocol-relative: looks same-origin, isn't.
        "//evil.example/logo.png",
        # Not an image, and svg can carry script.
        "data:text/html;base64,PHNjcmlwdD4=",
        "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=",
        "logo.png",
    ],
)
def test_put_rejects_dangerous_brand_logo_url(value):
    client, session_factory = _client_and_session()
    response = client.put("/api/settings", json={"brand_logo_url": value})

    assert response.status_code == 422, value
    assert "brand_logo_url" in response.json()["detail"]
    with session_factory() as session:
        assert session.query(AppSetting).count() == 0


@pytest.mark.parametrize(
    "value",
    [
        "https://cdn.example.com/logo.png",
        "http://cdn.example.com/logo.png",
        "/static/logo.png",
        "data:image/png;base64,iVBORw0KGgo=",
        "",
    ],
)
def test_put_accepts_allowed_brand_logo_url(value):
    client, _ = _client_and_session()
    response = client.put("/api/settings", json={"brand_logo_url": value})
    assert response.status_code == 200, value
    assert response.json()["brand_logo_url"] == value


def test_put_rejects_overlong_brand_name():
    client, _ = _client_and_session()
    response = client.put("/api/settings", json={"brand_name": "x" * 65})
    assert response.status_code == 422
    assert "brand_name" in response.json()["detail"]


def test_put_rejects_brand_name_with_line_breaks():
    client, _ = _client_and_session()
    response = client.put("/api/settings", json={"brand_name": "Acme\nJobs"})
    assert response.status_code == 422


def test_put_strips_whitespace_from_brand_name():
    client, _ = _client_and_session()
    response = client.put("/api/settings", json={"brand_name": "  Acme Jobs  "})
    assert response.status_code == 200
    assert response.json()["brand_name"] == "Acme Jobs"


def test_empty_brand_name_resets_to_the_default():
    """The ruling: an empty brand_name is a *reset*, not an error — otherwise
    an operator could only get the stock wordmark back by retyping it."""
    client, _ = _client_and_session()
    client.put("/api/settings", json={"brand_name": "Acme Jobs"})

    response = client.put("/api/settings", json={"brand_name": "   "})

    assert response.status_code == 200
    assert response.json()["brand_name"] == "celery-manager"
    assert client.get("/api/settings").json()["brand_name"] == "celery-manager"


def test_put_rejects_explicit_null():
    """An absent field means "leave alone"; an explicit null is a value, and
    not a string. It must not be silently treated as absent."""
    client, _ = _client_and_session()
    response = client.put("/api/settings", json={"brand_name": None})
    assert response.status_code == 422


def test_a_rejected_key_does_not_partially_apply_the_payload():
    """Whole-payload validation: the good key must not land behind the 422."""
    client, session_factory = _client_and_session()
    response = client.put(
        "/api/settings",
        json={"brand_name": "Acme Jobs", "brand_logo_url": "javascript:alert(1)"},
    )

    assert response.status_code == 422
    with session_factory() as session:
        assert session.query(AppSetting).count() == 0
    assert client.get("/api/settings").json()["brand_name"] == "celery-manager"


# --- Auditing --------------------------------------------------------------


def test_put_writes_one_audit_row_per_changed_key():
    client, session_factory = _client_and_session()
    response = client.put(
        "/api/settings",
        json={"brand_name": "Acme Jobs", "brand_accent": "#00ffcc"},
    )

    assert response.status_code == 200
    with session_factory() as session:
        rows = session.query(AuditLog).order_by(AuditLog.id).all()
        assert len(rows) == 2
        assert {r.target_id for r in rows} == {"brand_name", "brand_accent"}
        for row in rows:
            assert row.username == "alice"
            assert row.action == "update"
            assert row.target_type == "setting"
        by_key = {r.target_id: r.details for r in rows}
        assert "Acme Jobs" in by_key["brand_name"]
        assert "#00ffcc" in by_key["brand_accent"]


def test_put_writes_no_audit_row_for_an_unchanged_value():
    """The settings form submits all three fields whether or not they were
    edited; auditing the untouched ones would bury the real changes."""
    client, session_factory = _client_and_session()
    client.put("/api/settings", json={"brand_name": "Acme Jobs"})

    client.put(
        "/api/settings",
        json={"brand_name": "Acme Jobs", "brand_accent": "#00ffcc", "brand_logo_url": ""},
    )

    with session_factory() as session:
        keys = [r.target_id for r in session.query(AuditLog).order_by(AuditLog.id).all()]
    # brand_name once (the first PUT), brand_accent once (the second).
    # brand_logo_url never — it was submitted at its existing default.
    assert keys == ["brand_name", "brand_accent"]


def test_resubmitting_the_default_stores_nothing():
    """Unchanged is measured against the *effective* value, so writing a key
    that already holds its default creates no row at all."""
    client, session_factory = _client_and_session()
    response = client.put("/api/settings", json={"brand_name": "celery-manager"})

    assert response.status_code == 200
    with session_factory() as session:
        assert session.query(AppSetting).count() == 0
        assert session.query(AuditLog).count() == 0


# --- The control layer, called directly ------------------------------------


def test_update_settings_rejects_unknown_keys_at_the_control_layer():
    _, session_factory = _app_and_session()
    with session_factory() as session:
        with pytest.raises(ValueError) as exc:
            update_settings(session, {"smtp_password": "hunter2"}, "alice")
        assert "unknown setting" in str(exc.value)
        assert get_settings(session) == DEFAULT_SETTINGS


def test_update_settings_returns_full_effective_settings():
    _, session_factory = _app_and_session()
    with session_factory() as session:
        result = update_settings(session, {"brand_accent": "#0ff"}, "alice")
        assert result == {
            "brand_name": "celery-manager",
            "brand_accent": "#0ff",
            "brand_logo_url": "",
        }


def test_get_settings_ignores_rows_for_unsupported_keys():
    """The response shape is defined by DEFAULT_SETTINGS, not by the table —
    a leftover row from a removed setting must not leak out of the pre-auth
    endpoint."""
    _, session_factory = _app_and_session()
    with session_factory() as session:
        session.add(AppSetting(key="legacy_secret", value="hunter2"))
        session.commit()
        assert get_settings(session) == DEFAULT_SETTINGS
