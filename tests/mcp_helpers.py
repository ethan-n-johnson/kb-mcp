"""Helpers shared by the MCP tests: raw JSON-RPC over the mounted `/mcp` route."""

from __future__ import annotations

import itertools
import json
from typing import Any

import httpx
from fastapi.testclient import TestClient

MCP_HEADERS = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
TOKEN = {"authorization": "Bearer testtoken"}

_ids = itertools.count(1)


def rpc(
    client: TestClient,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    path: str = "/mcp",
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """POST one JSON-RPC request to the MCP route without following redirects."""
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": next(_ids), "method": method}
    if params is not None:
        body["params"] = params
    return client.post(
        path, json=body, headers={**MCP_HEADERS, **(headers or {})}, follow_redirects=False
    )


def call(
    client: TestClient,
    tool: str,
    arguments: dict[str, Any] | None = None,
    *,
    token: bool = False,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Call a tool and return the JSON-RPC `result` (`content`, `isError`, `structuredContent`)."""
    merged = {**(TOKEN if token else {}), **(headers or {})}
    resp = rpc(client, "tools/call", {"name": tool, "arguments": arguments or {}}, headers=merged)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "error" not in body, body
    return body["result"]


def ok(result: dict[str, Any]) -> dict[str, Any]:
    """The structured payload of a successful tool result."""
    assert not result["isError"], result
    return result["structuredContent"]


def error_text(result: dict[str, Any]) -> str:
    """The message of a failed tool result."""
    assert result["isError"], result
    return "".join(block["text"] for block in result["content"] if block["type"] == "text")


def payload_json(result: dict[str, Any]) -> Any:
    """The text block of a successful result parsed as JSON (what a text-only client sees)."""
    return json.loads(result["content"][0]["text"])
