"""Serving the MCP tools over HTTP inside the FastAPI app.

The tools are mounted at `/mcp` (stateless, JSON responses, no sessions). Two
things in the installed `mcp` 2.x SDK need handling here:

- Starlette redirects `POST /mcp` to `/mcp/` (307) when a sub-app is mounted at
  `/mcp`. Behind CloudFront the redirect would point at the ALB's plain-HTTP
  host and break clients, so `McpPathMiddleware` rewrites the path before
  routing and both forms answer directly.
- `streamable_http_app` turns on DNS rebinding protection (it only accepts a
  `Host` of localhost) unless told otherwise. The service is reached through
  CloudFront and the ALB under other host names, and every write needs a bearer
  token that a browser never attaches by itself, so the check is switched off.
"""

from fastapi import FastAPI
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

MOUNT_PATH = "/mcp"


class McpPathMiddleware:
    """Rewrite `/mcp` to `/mcp/` so the mount answers without a redirect."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket") and scope["path"] == MOUNT_PATH:
            scope = dict(scope)
            scope["path"] = MOUNT_PATH + "/"
            if scope.get("raw_path"):
                scope["raw_path"] = scope["raw_path"].replace(
                    MOUNT_PATH.encode(), (MOUNT_PATH + "/").encode(), 1
                )
        await self.app(scope, receive, send)


def mount_mcp(app: FastAPI, mcp: MCPServer) -> None:
    """Mount `mcp` at `/mcp` on `app`.

    The caller must run `mcp.session_manager.run()` in the app's lifespan: a
    mounted sub-app's own lifespan is never started by Starlette.
    """
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/",
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    app.mount(MOUNT_PATH, mcp_app)
    app.add_middleware(McpPathMiddleware)
