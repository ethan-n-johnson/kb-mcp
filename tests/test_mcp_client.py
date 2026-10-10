"""The official MCP client against a real uvicorn server (no TestClient shortcuts).

This is the closest local stand-in for Claude Code: a real socket, the SDK's own
Streamable HTTP client (which refuses to follow cross-origin redirects), and the
client's bearer header set the way `claude mcp add --header` sets it.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any, Iterator

import httpx
import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from kb import config, db
from kb.main import create_app
from kb.mcp_tools import MCP_ACTOR, TOOL_NAMES
from tests.corpus import EBS, S3
from tests.fakes import FakeEmbedder, FakeReranker

QUERY = "How do I attach an EBS volume to a Linux instance?"


@pytest.fixture
def live_server(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Run the app (fake models, temp DB, token `test:testtoken`) on a free port; yield its base URL."""
    monkeypatch.setenv("KB_DB", str(db_path))
    monkeypatch.setenv("KB_MODELS", "on")
    monkeypatch.setenv("KB_DUP_THRESHOLD", "1.5")
    monkeypatch.setenv("KB_WRITE_TOKENS", "test:testtoken")
    monkeypatch.delenv("KB_BUCKET", raising=False)
    config.clear_token_cache()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(FakeEmbedder(), FakeReranker()),
            host="127.0.0.1", port=0, log_config=None, lifespan="on",
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "server did not start"
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=20)
        config.clear_token_cache()


def sdk_client(url: str, token: str | None = None, **kwargs: Any) -> Client:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    http = httpx2.AsyncClient(headers=headers, timeout=30)
    return Client(streamable_http_client(url, http_client=http), **kwargs)


def run(coro: Any) -> Any:
    return asyncio.run(asyncio.wait_for(coro, timeout=60))


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_sdk_client_lists_tools_and_searches(live_server: str, path: str, mode: str) -> None:
    async def work() -> tuple[Any, Any, Any]:
        async with sdk_client(live_server + path, "testtoken", mode=mode) as client:
            tools = await client.list_tools()
            saved = await client.call_tool("upload", {"title": "EBS", "content": EBS})
            found = await client.call_tool("search", {"query": QUERY})
            return tools, saved, found

    tools, saved, found = run(work())
    assert [t.name for t in tools.tools] == list(TOOL_NAMES)
    assert saved.structured_content["status"] == "created"
    assert found.structured_content["mode"] == "semantic"
    assert found.structured_content["results"][0]["doc_id"] == saved.structured_content["doc_id"]


def test_sdk_client_without_a_token_can_read_and_write(live_server: str) -> None:
    async def work() -> tuple[Any, Any, Any]:
        async with sdk_client(live_server + "/mcp") as client:
            listed = await client.call_tool("list_tags", {})
            saved = await client.call_tool("upload", {"title": "Open", "content": S3})
            got = await client.call_tool("get", {"doc_id": saved.structured_content["doc_id"]})
            return listed, saved, got

    listed, saved, got = run(work())
    assert not listed.is_error and listed.structured_content == {"tags": []}
    assert not saved.is_error and saved.structured_content["status"] == "created"
    assert got.structured_content["title"] == "Open"
    assert MCP_ACTOR == "mcp"


def test_curl_style_post_to_the_bare_path_is_not_redirected(live_server: str) -> None:
    """The acceptance check: `POST /mcp` answers 200 itself, with the 12 tools."""
    resp = httpx.post(
        live_server + "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"accept": "application/json, text/event-stream"},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert len(resp.json()["result"]["tools"]) == 12


def test_concurrent_calls_do_not_block_each_other(live_server: str) -> None:
    """Stateless mode: many clients at once, each with its own request."""

    async def one(i: int) -> Any:
        async with sdk_client(live_server + "/mcp", "testtoken") as client:
            return await client.call_tool("upload", {"title": f"Note {i}", "content": f"# Note {i}\n\nUnique body number {i}."})

    async def work() -> list[Any]:
        return list(await asyncio.gather(*(one(i) for i in range(5))))

    results = run(work())
    assert all(r.structured_content["status"] == "created" for r in results)
    conn = db.connect(config.get_settings().db_path)
    try:
        assert conn.execute("SELECT count(*) FROM docs").fetchone()[0] == 5
    finally:
        conn.close()
