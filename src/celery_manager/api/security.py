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


def _split_port(host: str) -> tuple[str, str | None]:
    """`host[:port]` -> (host, port). IPv6 literals are bracketed (`[::1]:80`),
    so a naive rpartition(":") would cut into the address itself."""
    if host.startswith("["):
        end = host.find("]")
        if end != -1 and host[end + 1 : end + 2] == ":":
            return host[: end + 1], host[end + 2 :]
        return host, None
    name, sep, port = host.rpartition(":")
    # More than one colon without brackets isn't a valid Host; leave it whole
    # so it can only ever match an identical string.
    if sep and ":" not in name:
        return name, port
    return host, None


def _normalize_origin_host(netloc: str, scheme: str) -> str:
    # The Origin carries its scheme, so only that scheme's default port is
    # implied: `http://x:443` really is port 443 and must keep it.
    host, port = _split_port(netloc.strip().lower())
    if port is None or port == _DEFAULT_PORTS.get(scheme):
        return host
    return f"{host}:{port}"


def _host_header_forms(value: str) -> set[str]:
    """The forms a Host / X-Forwarded-Host value may match an Origin in.

    Host carries no scheme, and behind a TLS terminator the scheme the app
    sees is not the one the browser used: an upstream LB terminates https
    and forwards to nginx, whose `proxy_set_header Host $host:$server_port`
    then reports `dash.example.com:80` for a page served as
    `https://dash.example.com`. So either default port is dropped here.
    This adds no new kind of accepted origin: a port-less `Host: x` already
    matches both `http://x` and `https://x`, since the scheme was never part
    of the comparison. A non-default port is kept: `https://x:8443` vs
    `Host: x` is still a mismatch (a proxy that drops the port should
    forward `$http_host`, not have the check relaxed).
    """
    raw = value.strip().lower()
    host, port = _split_port(raw)
    forms = {raw}
    if port in _DEFAULT_PORTS.values():
        forms.add(host)
    return forms


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
        origin_host = _normalize_origin_host(parts.netloc, parts.scheme)
        # A proxy chain appends to X-Forwarded-Host; the first entry is the
        # host the browser actually addressed.
        forwarded_host = (headers.get(b"x-forwarded-host") or "").split(",")[0]
        candidates = [headers.get(b"host"), forwarded_host]
        return any(origin_host in _host_header_forms(c) for c in candidates if c)

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
