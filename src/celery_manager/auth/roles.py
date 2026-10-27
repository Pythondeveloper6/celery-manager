"""Dashboard roles.

Three roles, each a strict superset of the one before:

- ``viewer``: read-only. Every GET (lists, details including args, kwargs,
  logs and tracebacks, analytics, the audit log, the live stream, CSV/JSON
  export). Cannot change anything.
- ``operator``: day-to-day controls. Retry, revoke and terminate tasks,
  start a task, pause and resume schedules and single occurrences, and the
  worker controls that are reversible from the dashboard (pool grow/shrink,
  start/stop consuming a queue, task rate limits).
- ``admin``: everything, including the controls that destroy data or can't
  be undone from the dashboard (queue purge, worker shutdown) and
  deployment-wide settings (branding).

The server is the authority: every state-changing route declares its
minimum role with :func:`celery_manager.api.deps.require_role`, and
``create_app()`` refuses to build an app with a state-changing route that
doesn't. The dashboard only hides controls cosmetically.
"""

from __future__ import annotations

VIEWER = "viewer"
OPERATOR = "operator"
ADMIN = "admin"

# Ordered least to most privileged; a role may do everything the roles
# before it may.
ROLES: tuple[str, ...] = (VIEWER, OPERATOR, ADMIN)

_RANK = {role: rank for rank, role in enumerate(ROLES)}


def validate_role(role: str) -> str:
    """Return ``role`` if it is a known role, else raise ValueError."""
    if role not in _RANK:
        raise ValueError(f"unknown role {role!r}; expected one of: {', '.join(ROLES)}")
    return role


def role_allows(role: str | None, required: str) -> bool:
    """True if ``role`` is at least ``required``.

    An unknown or missing role (a value written to the database by hand, or
    by a newer version) allows nothing beyond reads: it fails closed rather
    than being guessed upwards.
    """
    rank = _RANK.get(role or "", -1)
    return rank >= _RANK[validate_role(required)]
