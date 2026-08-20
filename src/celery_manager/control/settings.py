from __future__ import annotations

import re
from datetime import datetime, timezone

from celery_manager.control.audit import record as record_audit
from celery_manager.db.models import AppSetting

# The complete, closed set of settings this deployment stores server-side,
# with the value each one has until an operator overrides it.
#
# Closed on purpose, and worth defending: `GET /api/settings` is
# unauthenticated (see api/routes/settings.py for why), so every key that
# lives in this dict is world-readable to anyone who can reach the dashboard.
# A product name, a colour and a logo URL are non-sensitive *by construction*.
# The moment someone adds `smtp_password` or `broker_url` here, that stops
# being true — so a new key is a security decision, not a convenience one, and
# anything sensitive belongs behind auth in its own table, not here.
#
# Three keys and no "generic preferences framework": this is a v1 branding
# pass. Per-*user* preferences (light/dark theme) deliberately live in the
# browser's localStorage instead — they are a property of the person, not of
# the deployment, and must never land in this table.
DEFAULT_SETTINGS: dict[str, str] = {
    # The wordmark in the nav, on the login card, and in the browser tab.
    "brand_name": "celery-manager",
    # Empty means "use the built-in phosphor-cyan accent"; otherwise a hex colour.
    "brand_accent": "",
    # Empty means "use the built-in brand mark"; otherwise an image URL.
    "brand_logo_url": "",
}

# Length caps. brand_name is a wordmark that has to fit a nav bar, and the
# column is String(64) — a longer value would either be truncated by the
# database or raise on a strict backend, so reject it here where we can say
# why. The URL caps exist because `value` is Text (effectively unbounded) and
# this row is served to every unauthenticated visitor on the login screen: a
# 20 MB data: URI would be a self-inflicted denial of service.
MAX_BRAND_NAME_LENGTH = 64
MAX_URL_LENGTH = 2048
MAX_DATA_URI_LENGTH = 256 * 1024

# `#rgb` or `#rrggbb`, nothing else — no `rgb()`, no named colours, no
# `var(...)`. Not pedantry: this value is injected into a CSS custom property
# in the browser (`--accent: <value>`), so any character we let through lands
# inside a stylesheet. A permissive check would accept
# `red; background: url(https://evil/x)` and let a stored value rewrite the
# whole page — a stored CSS-injection vector, and with it exfiltration via
# background-image requests. An anchored allowlist of hex digits has no
# escape hatch, so the injection is impossible rather than merely filtered.
_HEX_COLOUR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

# Raster image types only for data: URIs. `image/svg+xml` is excluded even
# though it is a legitimate image type: an SVG is a document that can carry
# <script> and event handlers, and while browsers do not execute script for an
# SVG loaded through <img src>, the same string is one copy-paste away from a
# CSS `url()`, an <object>, or a direct navigation where it does execute.
# Nobody needs an inline SVG logo badly enough to take that bet.
_DATA_URI = re.compile(r"^data:image/(?:png|jpeg|jpg|gif|webp|avif);base64,[A-Za-z0-9+/]+={0,2}$")

# Anything that is not a printable, single-line string has no business in a
# wordmark and is a smuggling vector in a URL (a newline can split a header if
# one of these ever ends up in one; C0 controls render as nothing and hide the
# real value from the operator who set it).
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _require_str(key: str, value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string, got {type(value).__name__}")
    return value


def _validate_brand_name(value: object) -> str:
    """Trim, bound, and allow an explicit reset.

    Ruling on empty: an empty (or whitespace-only) `brand_name` is **allowed
    and means "reset to the default"** — it normalizes to
    DEFAULT_SETTINGS["brand_name"] and is stored as that literal value. The
    alternative, rejecting it with a 422, would leave an operator who wants
    the stock branding back with no way to get there through the API except
    by retyping the default string exactly. Storing the resolved default
    rather than an empty string also means `get_settings` can never hand the
    UI a blank wordmark, so no caller needs an `|| "celery-manager"` fallback.
    """
    text = _require_str("brand_name", value).strip()
    if not text:
        return DEFAULT_SETTINGS["brand_name"]
    if _CONTROL_CHARS.search(text):
        raise ValueError("brand_name must not contain control characters or line breaks")
    if len(text) > MAX_BRAND_NAME_LENGTH:
        raise ValueError(f"brand_name must be at most {MAX_BRAND_NAME_LENGTH} characters")
    # Not HTML-escaped here: this is stored data, and escaping belongs at the
    # point of rendering (the SPA sets it as a text node / document.title,
    # both of which are inert). Escaping on write would double-escape an
    # ampersand in a legitimate name like "Ops & Data".
    return text


def _validate_brand_accent(value: object) -> str:
    """Empty, or a `#rgb`/`#rrggbb` hex colour. See `_HEX_COLOUR` for why the
    pattern is this strict — the short version is that this string is written
    into a CSS custom property, so anything looser is CSS injection."""
    text = _require_str("brand_accent", value).strip()
    if not text:
        return ""
    if not _HEX_COLOUR.match(text):
        raise ValueError(
            "brand_accent must be empty or a hex colour like #0ff or #00ffcc"
        )
    return text


def _validate_brand_logo_url(value: object) -> str:
    """Empty, or an http(s) URL, a same-origin `/path`, or a raster data: URI.

    This is the most security-relevant check in the module: the value ends up
    in an `<img src>`, so `javascript:alert(1)` here is stored XSS against
    every user of the dashboard — including, because GET /api/settings is
    pre-auth, everyone who merely loads the login page.

    The check is an **allowlist of schemes**, never a denylist. A denylist
    ("reject javascript:") loses to the next encoding the parser accepts and
    the blocklist doesn't: `JaVaScRiPt:`, `java\\tscript:`, `%6aavascript:`,
    `vbscript:`, or a scheme nobody has thought about yet. An allowlist
    inverts the burden — an unknown scheme is rejected by default, so being
    wrong about a new one is a false negative (a logo that doesn't load)
    rather than a compromise.

    `//evil.example/logo.png` is rejected along with everything else: a
    protocol-relative URL looks like a same-origin path but isn't, so the
    relative-path branch requires a single leading slash.
    """
    text = _require_str("brand_logo_url", value).strip()
    if not text:
        return ""
    if _CONTROL_CHARS.search(text):
        raise ValueError("brand_logo_url must not contain control characters or line breaks")

    lowered = text.lower()
    if lowered.startswith("data:"):
        if len(text) > MAX_DATA_URI_LENGTH:
            raise ValueError(
                f"brand_logo_url data: URI must be at most {MAX_DATA_URI_LENGTH} bytes"
            )
        if not _DATA_URI.match(text):
            raise ValueError(
                "brand_logo_url data: URI must be a base64 raster image "
                "(png, jpeg, gif, webp, avif); svg is not accepted"
            )
        return text

    if len(text) > MAX_URL_LENGTH:
        raise ValueError(f"brand_logo_url must be at most {MAX_URL_LENGTH} characters")
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return text
    if text.startswith("/") and not text.startswith("//"):
        return text

    raise ValueError(
        "brand_logo_url must be empty, an http:// or https:// URL, a path "
        "starting with /, or a data:image/... URI"
    )


_VALIDATORS = {
    "brand_name": _validate_brand_name,
    "brand_accent": _validate_brand_accent,
    "brand_logo_url": _validate_brand_logo_url,
}

# How much of a value to keep in an audit entry. A data: URI logo can be a
# quarter of a megabyte; copying two of them into every audit row would turn
# the audit log into an image store. The key and the fact of the change are
# what an operator needs from the log; the current value is one GET away.
_AUDIT_VALUE_LIMIT = 200


def _for_audit(value: str) -> str:
    return value if len(value) <= _AUDIT_VALUE_LIMIT else value[:_AUDIT_VALUE_LIMIT] + "…"


def get_settings(session) -> dict[str, str]:
    """Every supported setting, stored values overlaid on the defaults.

    Always returns the full key set, so no caller ever has to handle a missing
    key or invent its own fallback — the defaults live in exactly one place.
    Rows for keys that are no longer supported (a setting we removed) are
    ignored rather than returned: the response shape is defined by
    DEFAULT_SETTINGS, not by whatever happens to be in the table.
    """
    effective = dict(DEFAULT_SETTINGS)
    for row in session.query(AppSetting).all():
        if row.key in effective and row.value is not None:
            effective[row.key] = row.value
    return effective


def update_settings(session, values: dict, username: str) -> dict[str, str]:
    """Upsert the supplied settings and return the full effective settings.

    Only the keys present in `values` are touched, so a caller can change one
    setting without having to read-modify-write the other two (and without
    racing another operator who is editing a different one).

    Writes one audit entry per *changed* key. Unchanged values are skipped
    entirely — the dashboard's settings form submits all three fields whether
    or not they were edited, so auditing every submitted key would bury the
    real changes under no-ops.
    """
    if not isinstance(values, dict):
        raise ValueError("settings payload must be an object")

    # Unknown keys are a hard error, not a silent no-op. Ignoring them would
    # let a typo ("brand_colour") return 200 with the change apparently
    # accepted and nothing actually stored — the caller has no way to tell
    # success from a black hole.
    unknown = sorted(set(values) - set(DEFAULT_SETTINGS))
    if unknown:
        raise ValueError(f"unknown setting(s): {', '.join(unknown)}")

    # Validate the whole payload before writing any of it. A payload with a
    # good brand_name and a hostile brand_logo_url must be rejected whole; a
    # partial apply would persist half a change behind a 422 and leave the
    # operator guessing which half. Iterated in DEFAULT_SETTINGS order so the
    # error message for a multi-problem payload is deterministic.
    cleaned = {
        key: _VALIDATORS[key](values[key])
        for key in DEFAULT_SETTINGS
        if key in values
    }

    current = get_settings(session)
    rows = {row.key: row for row in session.query(AppSetting).all()}

    now = datetime.now(timezone.utc)
    changed: list[tuple[str, str, str]] = []
    for key, new_value in cleaned.items():
        old_value = current[key]
        if new_value == old_value:
            # Compared against the *effective* value, so setting a key to the
            # default it already has by default is also a no-op — no row is
            # created and nothing is audited.
            continue
        row = rows.get(key)
        if row is None:
            row = AppSetting(key=key)
            session.add(row)
        row.value = new_value
        row.updated_at = now
        row.updated_by = username
        changed.append((key, old_value, new_value))

    # Commit the rows before auditing. `audit.record` commits internally, so
    # this is belt-and-braces — but it keeps the ordering honest if auditing
    # is ever made optional here the way it is in `control.tasks.start_task`.
    session.commit()

    for key, old_value, new_value in changed:
        record_audit(
            session, username, "update", "setting", key,
            {"old": _for_audit(old_value), "new": _for_audit(new_value)},
        )

    return get_settings(session)
