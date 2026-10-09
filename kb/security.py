"""HTTP hardening: request body limit, security headers, CORS.

Three pieces of ASGI middleware and one helper, wired in `kb.main.create_app`:

- `BodySizeLimitMiddleware` refuses bodies over 20 MB with 413, by the declared
  `Content-Length` and, for chunked requests that declare none, by counting the
  bytes as they arrive.
- `SecurityHeadersMiddleware` adds the Content-Security-Policy and friends to
  HTML responses (and `nosniff` to everything).
- `cors_origin` validates `KB_EXTENSION_ORIGIN` for Starlette's `CORSMiddleware`.
  With no origin configured no CORS middleware is installed and no CORS header
  is ever sent.
"""

from __future__ import annotations

import json
import re

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_BODY_BYTES = 20 * 1024 * 1024

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "img-src 'self' https: data:; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'"
)
REFERRER_POLICY = "no-referrer"

CORS_METHODS = ["GET", "POST", "PATCH", "DELETE"]
CORS_HEADERS = ["Authorization", "Content-Type"]

_ORIGIN_RE = re.compile(r"^[a-z][a-z0-9+.-]*://[^\s/*,]+$")


def cors_origin(value: str | None) -> str | None:
    """`value` if it is a single concrete origin (`scheme://host`), else None.

    A wildcard, `null`, a list, a path or a trailing slash would either open the
    write routes to every site or silently never match, so they are rejected.
    """
    if value and _ORIGIN_RE.match(value) and value != "null":
        return value
    return None


class BodyTooLarge(Exception):
    """Raised inside the app when a request body runs past the limit."""


def _json_response(status: int, error: str, detail: str, *, close: bool = False) -> list[Message]:
    body = json.dumps({"error": error, "detail": detail}).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    if close:
        headers.append((b"connection", b"close"))
    return [
        {"type": "http.response.start", "status": status, "headers": headers},
        {"type": "http.response.body", "body": body},
    ]


class BodySizeLimitMiddleware:
    """Answer 413 for any request body over `max_bytes`.

    A `Content-Length` over the limit is refused before the app runs. Without
    one (chunked transfer) the bytes are counted as the app reads them; past the
    limit the read fails, and whatever response the app then produced (FastAPI
    turns a failed body read into a 400, the MCP SDK into a JSON-RPC error) is
    replaced by the 413.
    """

    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    @property
    def _detail(self) -> str:
        return f"request body exceeds the {self.max_bytes // (1024 * 1024)} MB limit"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            try:
                size = int(declared)
                if size < 0:
                    raise ValueError(declared)
            except ValueError:
                await self._send_all(send, _json_response(400, "bad_request", "invalid Content-Length", close=True))
                return
            if size > self.max_bytes:
                await self._send_all(send, _json_response(413, "too_large", self._detail, close=True))
                return

        seen = 0
        exceeded = False
        replaced = False

        async def counting_receive() -> Message:
            nonlocal seen, exceeded
            if exceeded:
                raise BodyTooLarge
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_bytes:
                    exceeded = True
                    raise BodyTooLarge
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal replaced
            if exceeded:
                # The app answered after a failed read: send the 413 instead, once.
                if not replaced:
                    replaced = True
                    await self._send_all(send, _json_response(413, "too_large", self._detail, close=True))
                return
            await send(message)

        try:
            await self.app(scope, counting_receive, guarded_send)
        except BodyTooLarge:
            if not replaced:
                await self._send_all(send, _json_response(413, "too_large", self._detail, close=True))

    @staticmethod
    async def _send_all(send: Send, messages: list[Message]) -> None:
        for message in messages:
            await send(message)


class SecurityHeadersMiddleware:
    """Add `Content-Security-Policy` and `Referrer-Policy` to HTML responses.

    `X-Content-Type-Options: nosniff` goes on every response. Headers an inner
    layer already set are left alone.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                if headers.get("content-type", "").lower().startswith("text/html"):
                    headers.setdefault("Content-Security-Policy", CSP)
                    headers.setdefault("Referrer-Policy", REFERRER_POLICY)
            await send(message)

        await self.app(scope, receive, with_headers)
