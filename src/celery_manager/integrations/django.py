"""Sign in to celery-manager with your Django users.

    from celery_manager.integrations.django import django_authenticator

    config = CeleryManagerConfig(
        ...,
        authenticate_request=django_authenticator(),
        login_url="/admin/login/?next={next}",
    )

The dashboard process must have Django configured (``django.setup()`` has
run with the same settings, database and ``SECRET_KEY`` as the site the user
signed in to) and receive the site's session cookie, which it does when it is
served from the same host name: mounted in Django's ASGI app, or as its own
process behind the same domain (cookies are not port-specific, so
``localhost:8000`` and ``localhost:8001`` share them in development).
"""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module

from celery_manager.auth.external import ExternalUser
from celery_manager.auth.roles import ADMIN, OPERATOR, VIEWER


def staff_roles(user) -> str | None:
    """The default mapping: superusers are admins, other staff are
    operators, everyone else has no access. Staff is Django's "may use the
    admin site" flag, the closest thing Django has to "runs this deployment"
    out of the box. Use :func:`permission_roles` for finer control."""
    if user.is_superuser:
        return ADMIN
    if user.is_staff:
        return OPERATOR
    return None


def permission_roles(
    *, admin: str | None = None, operator: str | None = None, viewer: str | None = None
) -> Callable[[object], str | None]:
    """A ``role_for`` that gives each user the highest role whose Django
    permission they have (``"app_label.codename"``, granted directly or
    through a group), or no access.

    The permissions are yours to define, on any model of your project, e.g.::

        class Meta:
            permissions = [
                ("celery_manager_admin", "Can administer celery-manager"),
                ("celery_manager_operate", "Can operate celery-manager"),
                ("celery_manager_view", "Can view celery-manager"),
            ]

    Django's ``has_perm`` is True for every permission of an active
    superuser, so superusers are always admins under this mapping.
    """
    if admin is None and operator is None and viewer is None:
        raise ValueError("permission_roles() needs at least one of admin, operator, viewer")
    ladder = [(role, perm) for role, perm in ((ADMIN, admin), (OPERATOR, operator), (VIEWER, viewer)) if perm]

    def role_for(user) -> str | None:
        for role, perm in ladder:
            if user.has_perm(perm):
                return role
        return None

    return role_for


def _user_from_session(session_key: str):
    """The active user a Django session key belongs to, or None.

    The same checks as ``django.contrib.auth.get_user()`` (the backend must
    still be configured, the session's auth hash must match the user's, so a
    password change on the Django side ends dashboard access too), but
    strictly read-only: Django's version flushes or re-keys the session in
    some cases, and a re-keyed session whose new cookie never reaches the
    browser would sign the user out of the Django site itself. This runs
    outside Django's request cycle and must never write to the session.
    """
    from django.conf import settings
    from django.contrib.auth import (
        BACKEND_SESSION_KEY,
        HASH_SESSION_KEY,
        SESSION_KEY,
        get_user_model,
        load_backend,
    )
    from django.utils.crypto import constant_time_compare

    # A malformed or expired key loads as an empty session, not an error.
    session = import_module(settings.SESSION_ENGINE).SessionStore(session_key)
    try:
        user_id = get_user_model()._meta.pk.to_python(session[SESSION_KEY])
        backend_path = session[BACKEND_SESSION_KEY]
    except KeyError:
        return None
    if backend_path not in settings.AUTHENTICATION_BACKENDS:
        return None
    user = load_backend(backend_path).get_user(user_id)
    # ModelBackend.get_user already drops inactive users; a custom backend
    # may not, and a deactivated account must not keep dashboard access.
    if user is None or not getattr(user, "is_active", True):
        return None
    if hasattr(user, "get_session_auth_hash"):
        session_hash = session.get(HASH_SESSION_KEY)
        if not session_hash:
            return None
        candidates = [user.get_session_auth_hash()]
        if hasattr(user, "get_session_auth_fallback_hash"):
            # Hashes under SECRET_KEY_FALLBACKS, so rotating Django's key
            # doesn't sign everyone out of the dashboard either.
            candidates.extend(user.get_session_auth_fallback_hash())
        if not any(constant_time_compare(session_hash, candidate) for candidate in candidates):
            return None
    return user


def django_authenticator(
    *,
    role_for: Callable[[object], str | None] = staff_roles,
    username_for: Callable[[object], str] | None = None,
) -> Callable[[object], ExternalUser | None]:
    """An ``authenticate_request`` for ``CeleryManagerConfig`` that signs in
    whoever is signed in to the Django site.

    role_for: maps a Django user to ``"viewer"``, ``"operator"``,
        ``"admin"`` or None (no access). Defaults to :func:`staff_roles`;
        see :func:`permission_roles` for a permission-based one.
    username_for: the name shown in the dashboard and audit log. Defaults to
        ``user.get_username()``.
    """

    def authenticate(request) -> ExternalUser | None:
        from django.conf import settings
        from django.db import close_old_connections

        session_key = request.cookies.get(settings.SESSION_COOKIE_NAME)
        if not session_key:
            return None
        # What Django's own request_started/request_finished signals do
        # around every request: drop connections that are past
        # CONN_MAX_AGE or broken. This runs in a reused threadpool thread
        # that Django's request cycle never sees, so without it each thread
        # would hold its connection open forever.
        close_old_connections()
        try:
            user = _user_from_session(session_key)
            if user is None:
                return None
            username = username_for(user) if username_for is not None else user.get_username()
            return ExternalUser(username=username, role=role_for(user))
        finally:
            close_old_connections()

    return authenticate
