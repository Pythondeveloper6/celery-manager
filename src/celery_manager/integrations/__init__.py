"""Sign in to the dashboard with the host application's own users.

Set ``CeleryManagerConfig.authenticate_request`` to a callable that takes the
Starlette ``Request`` and returns an :class:`ExternalUser` (or None when the
request isn't signed in to the host). Ready-made ones:

- :func:`celery_manager.integrations.django.django_authenticator` reads the
  Django session cookie and loads the user through Django's own session
  engine and auth backend.
- :func:`celery_manager.integrations.flask.flask_authenticator` reads Flask's
  signed session cookie with the Flask app's own secret and session
  interface, and loads the user with your ``load_user`` (Flask-Login's user
  loader works as-is).

For FastAPI or anything else, write the callable yourself: verify your own
cookie or token from ``request`` and map the user to a role. See
docs/integrations.md for each, with the security notes.

Each helper imports its framework only when it runs, so celery-manager
doesn't depend on Django or Flask.
"""

from celery_manager.auth.external import ExternalUser

__all__ = ["ExternalUser"]
