from __future__ import annotations

import json
from urllib.parse import urlsplit

# Methods that can change state. GET/HEAD/OPTIONS are never checked: every
# GET endpoint is read-only, and SameSite=lax already sends the cookie on
# top-level cross-site GETs anyway, so checking them would buy nothing.
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_DEFAULT_PORTS = {"http": "80", "https": "443"}

_SECURITY_HEADERS = [
    # The dashboard has one-click destructive controls (terminate, retry,
    # rebrand), which is exactly what clickjacking abuses. Both headers: CSP
    # frame-ancestors is the standard, X-Frame-Options covers older browsers.
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", b"frame-ancestors 'none'"),
]


def _normalize_host(host: str, scheme: str) -> str:
    host = host.strip().lower()
    default_port = _DEFAULT_PORTS.get(scheme)
    if default_port and host.endswith(f":{default_port}"):
        host = host[: -len(default_port) - 1]
    return host


class SecurityMiddleware:
    """Pure ASGI (not BaseHTTPMiddleware) so it never buffers a response —
    the SSE stream passes straight through.

    CSRF: SameSite=lax keeps the session cookie off cross-site POSTs in
    current browsers, but not every browser in use enforces it, and a
    same-site-but-cross-origin page (a sibling subdomain) is not covered by
    SameSite at all. So a state-changing request whose `Origin` names a
    different host than the one it was sent to is refused with a 403.

    A request with no `Origin` is allowed: browsers always send it on
    cross-origin POST/PUT/PATCH/DELETE, so its absence means a non-browser
    client (curl, a script, the test client), which can't be tricked into
    carrying a victim's cookie. `X-Forwarded-Host` is accepted as an
    alternative match for proxies that rewrite `Host`; a cross-origin page
    can't set that header without a CORS preflight this app never approves.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope["method"] in _UNSAFE_METHODS and not self._origin_allowed(scope):
            await self._reject(send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                headers.extend((name, value) for name, value in _SECURITY_HEADERS if name not in present)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)

    @staticmethod
    def _origin_allowed(scope) -> bool:
        headers = {}
        for name, value in scope.get("headers", []):
            headers.setdefault(name.lower(), value.decode("latin-1"))
        origin = headers.get(b"origin")
        if origin is None:
            return True
        parts = urlsplit(origin)
        # `Origin: null` (sandboxed iframes, file://, some redirects) has no
        # host to match, so it falls through to a mismatch.
        if not parts.scheme or not parts.netloc:
            return False
        origin_host = _normalize_host(parts.netloc, parts.scheme)
        # A proxy chain appends to X-Forwarded-Host; the first entry is the
        # host the browser actually addressed.
        forwarded_host = (headers.get(b"x-forwarded-host") or "").split(",")[0]
        candidates = [headers.get(b"host"), forwarded_host]
        return any(_normalize_host(c, parts.scheme) == origin_host for c in candidates if c)

    @staticmethod
    async def _reject(send):
        body = json.dumps({"detail": "cross-origin request refused"}, separators=(",", ":")).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("latin-1")),
                    *_SECURITY_HEADERS,
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
