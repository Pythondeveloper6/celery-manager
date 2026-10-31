"""`celery_manager.integrations.django`: signing in with a Django session.

The role mappings are tested with plain objects; the authenticator itself
against a real, minimally configured Django (skipped when Django isn't
installed), signing users in the way Django does and handing the resulting
`sessionid` cookie to the dashboard.
"""

import importlib.util
from types import SimpleNamespace

import pytest
from celery import Celery
from fastapi.testclient import TestClient

from celery_manager.api.app import create_app
from celery_manager.config import CeleryManagerConfig
from celery_manager.integrations.django import (
    django_authenticator,
    permission_roles,
    staff_roles,
)


def _user(*, superuser=False, staff=False, perms=()):
    return SimpleNamespace(is_superuser=superuser, is_staff=staff, has_perm=lambda perm: perm in perms)


# --- role mappings (no Django needed) -----------------------------------------


def test_staff_roles_default_mapping():
    assert staff_roles(_user(superuser=True, staff=True)) == "admin"
    assert staff_roles(_user(staff=True)) == "operator"
    assert staff_roles(_user()) is None


def test_permission_roles_gives_the_highest_role_held():
    role_for = permission_roles(admin="ops.cm_admin", operator="ops.cm_operate", viewer="ops.cm_view")
    assert role_for(_user(perms={"ops.cm_view", "ops.cm_admin"})) == "admin"
    assert role_for(_user(perms={"ops.cm_operate"})) == "operator"
    assert role_for(_user(perms={"ops.cm_view"})) == "viewer"
    assert role_for(_user(perms={"other.perm"})) is None


def test_permission_roles_needs_at_least_one_permission():
    with pytest.raises(ValueError):
        permission_roles()


def test_importing_the_helpers_imports_neither_framework():
    import os
    import subprocess
    import sys
    from pathlib import Path

    import celery_manager

    # Neither framework importable at all (a None entry makes `import x`
    # raise ImportError), as in a deployment that has neither installed.
    code = (
        "import sys; sys.modules.update(django=None, flask=None, werkzeug=None); "
        "import celery_manager.integrations.django, celery_manager.integrations.flask"
    )
    # This checkout's package, whatever an installed copy points at.
    env = {**os.environ, "PYTHONPATH": str(Path(celery_manager.__file__).parents[1])}
    subprocess.run([sys.executable, "-c", code], check=True, env=env)


# --- against a real Django ------------------------------------------------------


@pytest.fixture(scope="module")
def django_site(tmp_path_factory):
    pytest.importorskip("django")
    from django.conf import settings

    if settings.configured:
        pytest.skip("Django is already configured by another test module")
    installed = ["django.contrib.auth", "django.contrib.contenttypes", "django.contrib.sessions"]
    # One process can configure Django once. When the optional beat adapter is
    # installed, include it so its contract tests can share these settings.
    if importlib.util.find_spec("django_celery_beat") is not None:
        installed.append("django_celery_beat")
    settings.configure(
        SECRET_KEY="django-test-secret",
        INSTALLED_APPS=installed,
        DATABASES={
            "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": str(tmp_path_factory.mktemp("dj") / "db.sqlite3")}
        },
        PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
        USE_TZ=True,
        DEFAULT_AUTO_FIELD="django.db.models.AutoField",
    )
    import django
    from django.core.management import call_command

    django.setup()
    call_command("migrate", verbosity=0, interactive=False)
    return settings


def _django_login(user):
    """A session for ``user`` exactly as Django's login view leaves it."""
    from django.contrib.auth import login
    from django.contrib.sessions.backends.db import SessionStore
    from django.http import HttpRequest

    request = HttpRequest()
    request.session = SessionStore()
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    request.session.save()
    return request.session.session_key


def _make_user(username, **flags):
    from django.contrib.auth.models import User

    return User.objects.create_user(username=username, password="pw", **flags)


def _dashboard(**kwargs):
    config = CeleryManagerConfig(
        celery_app=Celery("test"),
        db_url="sqlite:///:memory:",
        secret_key="test-secret",
        authenticate_request=django_authenticator(**kwargs),
    )
    app, _ = create_app(config)
    return TestClient(app, base_url="https://testserver", raise_server_exceptions=False)


def _whoami(client, session_key):
    client.cookies.set("sessionid", session_key)
    return client.get("/api/whoami")


def test_django_superuser_staff_and_plain_users(django_site):
    client = _dashboard()
    root = _make_user("root", is_superuser=True, is_staff=True)
    staff = _make_user("staffer", is_staff=True)
    plain = _make_user("plain")

    assert _whoami(client, _django_login(root)).json() == {"username": "root", "role": "admin", "source": "external"}
    assert _whoami(client, _django_login(staff)).json()["role"] == "operator"
    refused = _whoami(client, _django_login(plain))
    assert refused.status_code == 403
    assert "(plain) has no access" in refused.json()["detail"]


def test_no_or_bogus_django_cookie_is_not_signed_in(django_site):
    client = _dashboard()
    assert client.get("/api/whoami").status_code == 401
    assert _whoami(client, "not-a-real-session-key").status_code == 401


def test_deactivated_or_deleted_django_user_loses_access_immediately(django_site):
    client = _dashboard()
    user = _make_user("leaver", is_staff=True)
    key = _django_login(user)
    assert _whoami(client, key).status_code == 200

    user.is_active = False
    user.save()
    assert _whoami(client, key).status_code == 401

    user.delete()
    assert _whoami(client, key).status_code == 401


def test_demotion_in_django_applies_on_the_next_request(django_site):
    client = _dashboard()
    user = _make_user("demoted", is_superuser=True, is_staff=True)
    key = _django_login(user)
    assert _whoami(client, key).json()["role"] == "admin"

    user.is_superuser = False
    user.save()
    assert _whoami(client, key).json()["role"] == "operator"


def test_password_change_in_django_ends_dashboard_access(django_site):
    client = _dashboard()
    user = _make_user("rotator", is_staff=True)
    key = _django_login(user)
    user.set_password("a-new-password")
    user.save()
    assert _whoami(client, key).status_code == 401


def test_reading_the_session_never_writes_to_it(django_site):
    from django.contrib.sessions.models import Session

    client = _dashboard()
    user = _make_user("reader", is_staff=True)
    key = _django_login(user)
    before = Session.objects.get(session_key=key).session_data
    # A mismatching hash is where Django's own get_user() would flush.
    user.set_password("changed")
    user.save()
    assert _whoami(client, key).status_code == 401
    assert Session.objects.get(session_key=key).session_data == before


def test_permission_based_mapping(django_site):
    from django.contrib.auth.models import Permission
    from django.contrib.contenttypes.models import ContentType

    content_type = ContentType.objects.get_for_model(Permission)
    operate = Permission.objects.create(codename="cm_operate", name="Can operate", content_type=content_type)
    client = _dashboard(role_for=permission_roles(operator="auth.cm_operate"), username_for=lambda u: u.email)
    user = _make_user("perm-holder", email="holder@example.com")

    assert _whoami(client, _django_login(user)).status_code == 403

    user.user_permissions.add(operate)
    user = type(user).objects.get(pk=user.pk)  # drop Django's per-instance permission cache
    assert _whoami(client, _django_login(user)).json() == {
        "username": "holder@example.com",
        "role": "operator",
        "source": "external",
    }
