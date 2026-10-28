"""Sign in to celery-manager with your Flask users.

    from celery_manager.integrations.flask import flask_authenticator

    config = CeleryManagerConfig(
        ...,
        authenticate_request=flask_authenticator(
            app,                           # your Flask app
            load_user=load_user,           # Flask-Login's user_loader works as-is
            role_for=lambda user: "admin" if user.is_admin else None,
        ),
        login_url="/login?next={next}",
    )

Meant for the dashboard mounted in the Flask app's own process (a2wsgi +
``DispatcherMiddleware``, see docs/integrations.md), where the browser sends
Flask's session cookie along with the dashboard's requests. A separate
process works too if it builds the same Flask app (same ``SECRET_KEY`` and
session settings) and is served under the same host name.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from celery_manager.auth.external import ExternalUser

logger = logging.getLogger(__name__)


def _default_username(user) -> str:
    for attribute in ("username", "email"):
        value = getattr(user, attribute, None)
        if isinstance(value, str) and value:
            return value
    get_id = getattr(user, "get_id", None)  # Flask-Login's UserMixin
    if callable(get_id):
        return str(get_id())
    raise TypeError(
        f"can't tell the username of {type(user).__name__}; pass username_for= to flask_authenticator()"
    )


def flask_authenticator(
    flask_app,
    *,
    load_user: Callable[[str], object | None],
    role_for: Callable[[object], str | None],
    username_for: Callable[[object], str] = _default_username,
    user_id_key: str = "_user_id",
) -> Callable[[object], ExternalUser | None]:
    """An ``authenticate_request`` for ``CeleryManagerConfig`` that signs in
    whoever is signed in to ``flask_app``.

    load_user: user id (as stored in the session) -> user object, or None.
        Runs inside a request context of ``flask_app``, so anything your
        Flask-Login ``user_loader`` uses (Flask-SQLAlchemy's ``db.session``,
        ``current_app``) works.
    role_for: user -> ``"viewer"``, ``"operator"``, ``"admin"`` or None (no
        access). Required: Flask has no built-in notion of staff to default to.
    username_for: the name shown in the dashboard and audit log. Defaults to
        the user's ``username``, else ``email``, else ``get_id()``.
    user_id_key: the session key holding the user id. ``"_user_id"`` is
        where Flask-Login keeps it.

    Only the session is consulted, not Flask-Login's "remember me" cookie:
    restoring a session from that cookie writes a new session, which only
    the Flask app's own response can deliver. Someone signed in only by that
    cookie is asked to open the Flask site once first (``login_url``).
    """
    if flask_app.config.get("SESSION_COOKIE_SAMESITE") is None:
        # The dashboard's Origin check still refuses cross-site writes (see
        # docs/integrations.md), but a cookie that is also SameSite is the
        # second, independent layer every other credential here has.
        logger.warning(
            "celery-manager: the Flask app's session cookie has no SameSite attribute; "
            'set SESSION_COOKIE_SAMESITE = "Lax" now that it also signs people in to the dashboard'
        )

    def authenticate(request) -> ExternalUser | None:
        from flask import session
        from werkzeug.test import EnvironBuilder

        cookie_name = flask_app.session_interface.get_cookie_name(flask_app)
        if cookie_name not in request.cookies:
            return None
        # A request context of the Flask app, carrying this request's
        # headers, so its session interface reads the cookie exactly as it
        # would for one of its own requests (signature, max age, a
        # server-side session store), and load_user runs with the app's
        # extensions available. No response is ever made from it, so the
        # Flask session is never written to.
        environ = EnvironBuilder(
            path="/",
            # The request's own body isn't part of this (it may still be
            # streaming), so neither are the headers that describe it.
            headers=[
                (name, value)
                for name, value in request.headers.items()
                if name.lower() not in ("content-length", "content-type", "transfer-encoding")
            ],
            environ_base={"REMOTE_ADDR": request.client.host if request.client else ""},
        ).get_environ()
        with flask_app.request_context(environ):
            user_id = session.get(user_id_key)
            if user_id is None:
                return None
            user = load_user(str(user_id))
            if user is None:
                return None
            # Flask-Login's UserMixin; a deactivated account gets no access.
            if getattr(user, "is_active", True) is False:
                return None
            return ExternalUser(username=username_for(user), role=role_for(user))

    return authenticate
