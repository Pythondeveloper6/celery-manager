"""Call a FastAPI/ASGI app in-process, with no HTTP server and no httpx."""
from __future__ import annotations

import asyncio
import json
from urllib.parse import urlencode


class InProcessClient:
    """Minimal ASGI client: one request in, ``(status, parsed body)`` out."""

    def __init__(self, app):
        self.app = app

    def request(self, method: str, path: str, *, params: dict | None = None, body=None, headers: dict | None = None):
        return asyncio.run(self._request(method, path, params, body, headers or {}))

    async def _request(self, method, path, params, body, headers):
        query = urlencode([(k, str(v).lower() if isinstance(v, bool) else str(v))
                           for k, v in (params or {}).items() if v is not None])
        payload = json.dumps(body).encode() if body is not None else b""
        raw_headers = [(b"host", b"mcp.local"), (b"accept", b"application/json")]
        if body is not None:
            raw_headers += [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
        raw_headers += [(k.lower().encode(), v.encode("latin-1", "ignore")) for k, v in headers.items()]
        scope = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method.upper(),
            "scheme": "https", "path": path, "raw_path": path.encode(), "query_string": query.encode(),
            "headers": raw_headers, "client": ("127.0.0.1", 0), "server": ("mcp.local", 443), "root_path": "",
        }
        sent = False
        status = 500
        chunks: list[bytes] = []

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": payload, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        await self.app(scope, receive, send)
        raw = b"".join(chunks)
        try:
            return status, json.loads(raw) if raw else None
        except ValueError:
            return status, raw.decode("utf-8", "replace")
