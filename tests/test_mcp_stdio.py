"""MCP over stdio: spawn `python -m kb.cli mcp-stdio` and talk to it for real.

The child gets a temp database and either `KB_MODELS=off` or `KB_FAKE_MODELS=1`
(the test doubles, so the semantic path runs without downloading models). Its
stderr goes to a file so the tests can check that logging stays off stdout.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client

from kb import db, service
from kb.mcp_tools import MCP_ACTOR, TOOL_NAMES
from kb.schemas import UploadRequest
from tests.corpus import EBS, S3

REPO = Path(__file__).parent.parent
QUERY = "How do I attach an EBS volume to a Linux instance?"


@pytest.fixture
def stdio_db(tmp_path: Path) -> Path:
    """A database with two documents stored before the server starts (no vectors yet)."""
    path = tmp_path / "stdio" / "kb.db"
    conn = db.connect(path)
    db.init_schema(conn)
    try:
        for md in (EBS, S3):
            service.upload(conn, UploadRequest(content=md), "seed")
    finally:
        conn.close()
    return path


def child_env(db_path: Path, **extra: str) -> dict[str, str]:
    env = {
        "KB_DB": str(db_path),
        "KB_MODELS": "off",
        "KB_WRITE_TOKENS": "test:testtoken",
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
    }
    env.update(extra)
    return env


def params(env: dict[str, str]) -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable, args=["-m", "kb.cli", "mcp-stdio"], env=env, cwd=str(REPO)
    )


def run_client(env: dict[str, str], stderr_file: Path, work: Any) -> Any:
    """Start the server with `env`, run `await work(client)` and return its result."""

    async def main() -> Any:
        with stderr_file.open("w") as errlog:
            async with Client(stdio_client(params(env), errlog=errlog)) as client:
                return await work(client)

    return asyncio.run(asyncio.wait_for(main(), timeout=60))


def test_lists_the_eleven_tools_and_searches_a_temp_db(stdio_db: Path, tmp_path: Path) -> None:
    async def work(client: Client) -> tuple[Any, Any]:
        tools = await client.list_tools()
        found = await client.call_tool("search", {"query": QUERY})
        return tools, found

    tools, found = run_client(child_env(stdio_db), tmp_path / "err.log", work)
    assert [t.name for t in tools.tools] == list(TOOL_NAMES)
    assert not found.is_error
    data = found.structured_content
    assert data["mode"] == "keyword"  # KB_MODELS=off
    assert data["results"] and data["results"][0]["title"] == "Amazon EBS volumes"


def test_semantic_search_with_the_test_doubles(stdio_db: Path, tmp_path: Path) -> None:
    """Startup embeds the stored chunks (backfill) and builds the index, then search is semantic."""

    async def work(client: Client) -> Any:
        return (await client.call_tool("search", {"query": QUERY})).structured_content

    data = run_client(
        child_env(stdio_db, KB_MODELS="on", KB_FAKE_MODELS="1"), tmp_path / "err.log", work
    )
    assert data["mode"] == "semantic"
    assert data["results"][0]["title"] == "Amazon EBS volumes"
    assert data["results"][0]["heading_path"].endswith("Linux")
    log = (tmp_path / "err.log").read_text()
    assert "search startup complete" in log and '"backfilled": ' in log


def test_write_tools_use_kb_token(stdio_db: Path, tmp_path: Path) -> None:
    new_doc = {"title": "Stdio note", "content": "# Stdio note\n\nSaved through the stdio transport."}

    async def work(client: Client) -> Any:
        result = await client.call_tool("upload", new_doc)
        got = await client.call_tool("get", {"doc_id": result.structured_content["doc_id"]})
        return result, got

    result, got = run_client(child_env(stdio_db, KB_TOKEN="testtoken"), tmp_path / "err.log", work)
    assert not result.is_error and result.structured_content["status"] == "created"
    assert got.structured_content["title"] == "Stdio note"
    conn = db.connect(stdio_db)
    try:
        last = conn.execute("SELECT token_name, action FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    assert (last["token_name"], last["action"]) == ("test", "upload")


@pytest.mark.parametrize("token", [None, "wrong"], ids=["no-token", "wrong-token"])
def test_write_tools_work_without_a_valid_kb_token(stdio_db: Path, tmp_path: Path, token: str | None) -> None:
    extra = {} if token is None else {"KB_TOKEN": token}

    async def work(client: Client) -> Any:
        return await client.call_tool("upload", {"title": "Open", "content": "# Open\n\nsaved without a token"})

    result = run_client(child_env(stdio_db, **extra), tmp_path / "err.log", work)
    assert not result.is_error and result.structured_content["status"] == "created"
    conn = db.connect(stdio_db)
    try:
        assert conn.execute("SELECT count(*) FROM docs").fetchone()[0] == 3
        last = conn.execute("SELECT token_name FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    assert last["token_name"] == MCP_ACTOR


def test_stdout_carries_only_protocol_and_logs_go_to_stderr(stdio_db: Path) -> None:
    """Drive the process by hand: every stdout line is a JSON-RPC message, every stderr line a JSON log."""
    env = {**os.environ, **child_env(stdio_db, KB_MODELS="on", KB_FAKE_MODELS="1", KB_TOKEN="testtoken")}
    init = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "pytest", "version": "0"},
        },
    }
    messages = [
        init,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "search", "arguments": {"query": QUERY}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
            "name": "upload", "arguments": {"title": "Loud", "content": "# Loud\n\nA note with enough words to store."}}},
    ]
    proc = subprocess.Popen(
        [sys.executable, "-m", "kb.cli", "mcp-stdio"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=REPO, env=env,
    )
    watchdog = threading.Timer(60, proc.kill)  # a hung child fails the test instead of blocking it
    watchdog.start()
    out_lines: list[str] = []
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write("".join(json.dumps(m) + "\n" for m in messages))
        proc.stdin.flush()
        wanted = {1, 2, 3, 4}
        seen: set[int] = set()
        while seen != wanted:  # keep stdin open until every request is answered
            line = proc.stdout.readline()
            assert line, "server closed stdout early"
            out_lines.append(line)
            message = json.loads(line)  # a stray print fails here
            if "id" in message:
                seen.add(message["id"])
        proc.stdin.close()
        proc.stdin = None
        rest, stderr = proc.communicate(timeout=30)
        out_lines += rest.splitlines()
    finally:
        watchdog.cancel()
        proc.kill()
    out_lines = [line for line in out_lines if line.strip()]
    decoded = [json.loads(line) for line in out_lines]  # raises if anything else was printed
    assert all(msg.get("jsonrpc") == "2.0" for msg in decoded)
    by_id = {msg["id"]: msg for msg in decoded if "id" in msg}
    assert set(by_id) == {1, 2, 3, 4}
    assert len(by_id[2]["result"]["tools"]) == 12
    assert by_id[3]["result"]["structuredContent"]["mode"] == "semantic"
    assert by_id[4]["result"]["isError"] is False
    err_lines = [line for line in stderr.splitlines() if line.strip()]
    assert err_lines
    logs = [json.loads(line) for line in err_lines]  # structured logs only, on stderr
    assert {"ts", "level", "logger", "msg"} <= set(logs[0])
    assert any(entry["msg"] == "search startup complete" for entry in logs)
    assert "testtoken" not in "".join(out_lines) and "testtoken" not in stderr
    assert proc.returncode == 0


def test_help_lists_the_command() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "kb.cli", "--help"], capture_output=True, text=True, timeout=60, cwd=REPO
    )
    assert "mcp-stdio" in proc.stdout
