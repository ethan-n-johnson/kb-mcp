"""Find the real client address behind CloudFront and the ALB.

The rate limit is keyed by client address, so the address must be one the
caller cannot choose. uvicorn's `--proxy-headers` with `--forwarded-allow-ips *`
takes the LEFT-most `X-Forwarded-For` entry, which is whatever the caller sent;
a client could rotate that header and never be limited. This middleware counts
from the RIGHT instead, where only trusted proxies append:

    X-Forwarded-For: <anything the caller sent>, <viewer>, <CloudFront edge>
                                                   ^ CloudFront    ^ ALB

`KB_PROXY_HOPS` is the number of proxies in front of the container that each
append one entry (2 behind CloudFront and the ALB, 1 behind the ALB alone, 0
when the app is reached directly). The client is entry number `hops` counted
from the right. When the header is missing, shorter than that, or the entry is
not an IP address, the socket peer is kept (never the left-most entry).
"""

from __future__ import annotations

import ipaddress

from starlette.types import ASGIApp, Receive, Scope, Send


def client_from_forwarded(header: str, hops: int) -> str | None:
    """The address `hops` entries from the right of an `X-Forwarded-For` value, else None."""
    if hops < 1:
        return None
    entries = [part.strip() for part in header.split(",") if part.strip()]
    if len(entries) < hops:
        return None
    candidate = entries[-hops]
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


class ClientIpMiddleware:
    """Replace `scope["client"]` with the address found by `client_from_forwarded`."""

    def __init__(self, app: ASGIApp, hops: int = 0) -> None:
        self.app = app
        self.hops = hops

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.hops > 0 and scope["type"] in ("http", "websocket"):
            values = [v for k, v in scope["headers"] if k == b"x-forwarded-for"]
            if values:
                header = b", ".join(values).decode("latin1")
                found = client_from_forwarded(header, self.hops)
                if found is not None:
                    scope = dict(scope)
                    scope["client"] = (found, 0)
        await self.app(scope, receive, send)
