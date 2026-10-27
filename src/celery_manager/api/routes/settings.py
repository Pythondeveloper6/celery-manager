from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from celery_manager.api.deps import require_admin
from celery_manager.control.settings import get_settings, update_settings

router = APIRouter(prefix="/api/settings")


@router.get("")
def read_settings(request: Request):
    """The deployment's branding. **Deliberately unauthenticated.**

    The login screen has to render this deployment's branding — its wordmark,
    its accent, its logo — and the login screen is by definition pre-auth. An
    authenticated endpoint here would mean the dashboard shows stock branding
    until the moment you sign in, which defeats the feature for the one screen
    an operator's users see most often.

    That is safe only because the payload is non-sensitive by construction: a
    product name, a hex colour and an image URL, and nothing else, because
    `control.settings.DEFAULT_SETTINGS` is a closed allowlist. It is why that
    allowlist must stay closed — see the comment on it before adding a key.

    `updated_by` and `updated_at` are deliberately *not* returned. They are
    the one genuinely sensitive thing this table holds: `updated_by` is a
    valid username, and handing an unauthenticated visitor a real account name
    turns a password-guessing attack from two unknowns into one. Nothing in
    the UI needs them, so they are omitted rather than served behind a second
    authenticated route that would exist only to be tidy.
    """
    session_factory = request.app.state.cm_session_factory
    with session_factory() as session:
        return get_settings(session)


class SettingsUpdate(BaseModel):
    """PUT body. Every field optional, so a caller can send just the one it
    is changing rather than read-modify-writing the whole set.

    `extra="forbid"` so an unrecognized key is a 422 from pydantic, matching
    what `update_settings` does with unknown keys when it is called directly
    from Python. Silently dropping an unknown field would let a typo look like
    a successful save.
    """

    model_config = ConfigDict(extra="forbid")

    brand_name: str | None = None
    brand_accent: str | None = None
    brand_logo_url: str | None = None


@router.put("")
def write_settings(payload: SettingsUpdate, request: Request, username: str = Depends(require_admin)):
    """Update branding. Admin only; returns the new effective settings.

    Admin rather than operator: branding is deployment-wide, so one account
    changing it changes the dashboard for everyone. Every change is still
    attributed in the audit log.
    """
    # `exclude_unset` is what makes a partial update possible: it keeps the
    # difference between "brand_accent was not in the JSON" (leave it alone)
    # and "brand_accent was sent as null" (an explicit value, which the
    # validators reject as a non-string rather than silently ignoring).
    values = payload.model_dump(exclude_unset=True)

    session_factory = request.app.state.cm_session_factory
    with session_factory() as session:
        try:
            return update_settings(session, values, username)
        except ValueError as exc:
            # 422, not 400: this is a well-formed request whose *content*
            # failed validation, which is the same code FastAPI/pydantic
            # already returns for a bad body — so a client has one error shape
            # to handle, not two. The message is human-readable on purpose;
            # it is rendered straight into the settings form.
            raise HTTPException(status_code=422, detail=str(exc))
