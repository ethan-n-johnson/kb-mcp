"""MCP tools over HTTP at `/mcp`: stateless JSON-RPC, no redirects, no token needed.

Requests go straight to the mounted route with raw JSON-RPC (the way the
acceptance `curl` does), so nothing depends on a client library. The SDK
client is exercised separately in `test_mcp_client.py`.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from kb import service
from kb.ingest.fetch import FetchResult
from kb.mcp_tools import MCP_ACTOR, TOOL_NAMES, build_mcp, mcp_actor
from tests.conftest import OpenApp
from tests.corpus import EBS, S3
from tests.helpers import load_corpus, upload
from tests.mcp_helpers import call, error_text, ok, payload_json, rpc
from tests.ops_helpers import EBS_EDITED, add_gap, audit_rows, backdate, rows, scalar

PLAN = Path(__file__).parent.parent / "docs" / "PLAN.md"

WRITE_TOOLS = ["upload", "upload_file", "update", "delete", "merge", "ingest_url", "verify"]
READ_TOOLS = ["search", "get", "list_tags", "stale", "gaps"]

# One call per write tool, used to show that none of them needs a token.
WRITE_CALLS: dict[str, dict[str, Any]] = {
    "upload": {"title": "Note", "content": "# Note\n\nsome text that is long enough to chunk"},
    "upload_file": {"filename": "guide.pdf"},
    "update": {"doc_id": 1, "title": "Renamed"},
    "delete": {"doc_id": 1},
    "merge": {"target_id": 1, "source_ids": [2]},
    "ingest_url": {"url": "https://docs.example.com/page"},
    "verify": {"doc_id": 1},
}


# --- the tool set matches PLAN.md ---------------------------------------------------------


def plan_table() -> dict[str, tuple[set[str], dict[str, Any], set[str]]]:
    """Parse the tool table in PLAN.md: tool -> (argument names, defaults, optional names).

    Cells look like `` `query`, `k=5`, `tags?` ``, `` `doc_id` or `chunk_id`, `include_chunks=false` ``
    or `none`. A `?` marks an optional argument without a default; the
    `doc_id` or `chunk_id` pair is optional too (exactly one is needed).
    """
    table: dict[str, tuple[set[str], dict[str, Any], set[str]]] = {}
    for line in PLAN.read_text().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 5 or not re.fullmatch(r"`\w+`", cells[0]) or cells[0] == "`MCP tool`":
            continue
        name = cells[0].strip("`")
        names: set[str] = set()
        defaults: dict[str, Any] = {}
        optional: set[str] = set()
        for token in re.findall(r"`([^`]+)`", cells[1]):
            if token == "none":
                continue
            if "=" in token:
                arg, raw = token.split("=")
                defaults[arg] = {"true": True, "false": False}.get(raw) if raw in ("true", "false") else int(raw)
                names.add(arg)
            elif token.endswith("?"):
                names.add(token[:-1])
                optional.add(token[:-1])
            else:
                names.add(token)
        if " or " in cells[1]:
            optional |= {"doc_id", "chunk_id"}
        table[name] = (names, defaults, optional)
    return table


def listed_tools(client: TestClient) -> dict[str, dict[str, Any]]:
    resp = rpc(client, "tools/list")
    assert resp.status_code == 200, resp.text
    return {t["name"]: t for t in resp.json()["result"]["tools"]}


def test_the_plan_table_parses_to_twelve_tools() -> None:
    table = plan_table()
    assert len(table) == 12
    assert table["search"] == ({"query", "k", "tags"}, {"k": 5}, {"tags"})
    assert table["get"][0] == {"doc_id", "chunk_id", "include_chunks"}
    assert table["list_tags"][0] == set()


def test_tool_names_and_arguments_match_plan_md_exactly(open_app: OpenApp) -> None:
    table = plan_table()
    with open_app(models=False) as client:
        tools = listed_tools(client)
    assert set(tools) == set(table) == set(TOOL_NAMES)
    for name, (names, defaults, optional) in table.items():
        schema = tools[name]["inputSchema"]
        properties = schema.get("properties", {})
        assert set(properties) == names, name  # no `ctx`, nothing extra
        required = set(schema.get("required", []))
        for arg, value in defaults.items():
            assert properties[arg]["default"] == value, f"{name}.{arg}"
            assert arg not in required
        for arg in optional:
            assert arg not in required, f"{name}.{arg} must be optional"
        # Arguments the table lists without a default or `?` are required, except
        # `upload.format`, which defaults to markdown (see PROGRESS.md).
        expected_required = names - set(defaults) - optional - ({"format"} if name == "upload" else set())
        assert required == expected_required, name


def test_descriptions_say_when_to_use_the_tool(open_app: OpenApp) -> None:
    with open_app(models=False) as client:
        tools = listed_tools(client)
    for name, tool in tools.items():
        text = tool["description"]
        assert len(text) > 80, name
        assert re.search(r"\b(Use|use|Check|before|when|After|Only|Fold)\b", text), name
        assert chr(0x2014) not in text and chr(0x2013) not in text, name
    assert "confidence" in tools["search"]["description"]
    assert "possible_duplicate" in tools["upload"]["description"]
    assert "force=true" in tools["upload"]["description"]
    assert "most frequent first" in tools["gaps"]["description"]
    for name in TOOL_NAMES:
        assert "token" not in tools[name]["description"], name


def test_read_only_hints_mark_reads_and_writes(open_app: OpenApp) -> None:
    with open_app(models=False) as client:
        tools = listed_tools(client)
    for name in READ_TOOLS:
        assert tools[name]["annotations"]["readOnlyHint"] is True, name
    for name in WRITE_TOOLS:
        assert tools[name]["annotations"]["readOnlyHint"] is False, name
    assert tools["delete"]["annotations"]["destructiveHint"] is True


# --- the route: 200 directly for both forms ------------------------------------------------


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
def test_tools_list_answers_directly_without_initialize_or_redirect(open_app: OpenApp, path: str) -> None:
    with open_app(models=False) as client:
        resp = rpc(client, "tools/list", path=path)
    assert resp.status_code == 200
    assert "location" not in resp.headers
    assert resp.headers["content-type"].startswith("application/json")  # JSON, not an event stream
    assert "mcp-session-id" not in resp.headers  # stateless
    assert [t["name"] for t in resp.json()["result"]["tools"]] == list(TOOL_NAMES)


@pytest.mark.parametrize("path", ["/mcp", "/mcp/"])
def test_initialize_works_on_both_paths(open_app: OpenApp, path: str) -> None:
    params = {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "0"},
    }
    with open_app(models=False) as client:
        resp = rpc(client, "initialize", params, path=path)
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert result["serverInfo"]["name"] == "kb"
    assert "tools" in result["capabilities"]
    assert "mcp-session-id" not in resp.headers


def test_the_host_header_is_not_restricted(open_app: OpenApp) -> None:
    """Behind CloudFront and the ALB the Host is not localhost; the SDK default would answer 421."""
    with open_app(models=False) as client:
        for host in ("kb-alb-123.us-east-1.elb.amazonaws.com", "d1234.cloudfront.net"):
            resp = rpc(client, "tools/list", headers={"host": host})
            assert resp.status_code == 200, (host, resp.text)


def test_rest_routes_still_work_next_to_the_mount(open_app: OpenApp) -> None:
    with open_app(models=False) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/api/tags").json() == {"tags": []}
        assert client.get("/api/nothing-here").status_code == 404


# --- reads need no token -------------------------------------------------------------------


def test_search_returns_results_like_the_rest_route(open_app: OpenApp) -> None:
    with open_app() as client:
        ids = load_corpus(client)
        query = "How do I attach an EBS volume to a Linux instance?"
        data = ok(call(client, "search", {"query": query, "k": 3}))
        rest = client.get("/api/search", params={"q": query, "k": 3}).json()
    assert data["mode"] == "semantic"
    assert data["results"][0]["doc_id"] == ids["ebs"]
    assert data["results"][0]["heading_path"].endswith("Linux")
    assert len(data["results"]) <= 3
    assert data == rest  # same shape and values as REST


def test_search_filters_by_tags_and_reports_a_gap(open_app: OpenApp) -> None:
    with open_app() as client:
        ids = load_corpus(client)
        tagged = ok(call(client, "search", {"query": "snapshots", "tags": ["storage"]}))
        none = ok(call(client, "search", {"query": "snapshots", "tags": ["no-such-tag"]}))
    assert {r["doc_id"] for r in tagged["results"]} <= {ids["ebs"], ids["s3"]}
    assert none["results"] == [] and none["confidence"] == "low"


def test_the_text_content_is_the_same_json(open_app: OpenApp) -> None:
    """Clients that ignore `structuredContent` read the JSON text block."""
    with open_app(models=False) as client:
        result = call(client, "list_tags")
    assert payload_json(result) == result["structuredContent"] == {"tags": []}


def test_get_stale_gaps_and_tags_without_a_token(open_app: OpenApp) -> None:
    with open_app() as client:
        ebs = upload(client, EBS, ["storage", "ec2"])
        old = upload(client, S3, ["storage"])
        backdate(client, old, 200)
        add_gap(client, "How do I rotate keys?")
        doc = ok(call(client, "get", {"doc_id": ebs, "include_chunks": True}))
        by_chunk = ok(call(client, "get", {"chunk_id": doc["chunks"][1]["id"]}))
        tags = ok(call(client, "list_tags"))
        stale = ok(call(client, "stale", {"days": 90}))
        gaps = ok(call(client, "gaps"))
    assert doc["title"] == "Amazon EBS volumes" and doc["tags"] == ["ec2", "storage"]
    assert doc["chunks"] and doc["content_md"].startswith("# Amazon EBS")
    assert by_chunk["id"] == ebs
    assert [c["id"] for c in by_chunk["chunks"] if c["matched"]] == [doc["chunks"][1]["id"]]
    assert tags["tags"][0] == {"tag": "storage", "count": 2}
    assert [d["doc_id"] for d in stale["docs"]] == [old]
    assert [g["query"] for g in gaps["gaps"]] == ["How do I rotate keys?"]


def test_limits_and_days_are_validated(open_app: OpenApp) -> None:
    with open_app(models=False) as client:
        bad = call(client, "stale", {"days": -1})
        too_many = call(client, "gaps", {"limit": 500})
    assert bad["isError"] and too_many["isError"]


# --- anticipated failures are readable error results ---------------------------------------


@pytest.mark.parametrize(
    ("tool", "arguments", "expected"),
    [
        ("get", {"doc_id": 999}, "not_found: doc 999 not found"),
        ("get", {"chunk_id": 999}, "not_found: chunk 999 not found"),
        ("get", {}, "bad_request: give exactly one of doc_id and chunk_id"),
        ("search", {"query": "   "}, "invalid_request: query must not be empty"),
    ],
)
def test_read_errors_name_the_code(open_app: OpenApp, tool: str, arguments: dict, expected: str) -> None:
    with open_app(models=False) as client:
        result = call(client, tool, arguments)
    assert error_text(result).endswith(expected)


def test_missing_required_arguments_are_an_error_result(open_app: OpenApp) -> None:
    with open_app(models=False) as client:
        result = call(client, "search", {})
    assert result["isError"]
    assert "query" in error_text(result)


# --- write tools: no token needed ---------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"authorization": "Bearer wrong-token"},
        {"authorization": "Basic testtoken"},
        {"authorization": "Bearer"},
        {"authorization": "testtoken"},
    ],
    ids=["missing", "wrong", "wrong-scheme", "empty", "no-scheme"],
)
def test_write_tools_run_without_a_valid_token_and_audit_as_mcp(
    open_app: OpenApp, headers: dict[str, str]
) -> None:
    with open_app() as client:
        first, second = upload(client, EBS), upload(client, S3)
        order = ["upload", "upload_file", "update", "verify", "merge", "delete"]
        results = {tool: call(client, tool, WRITE_CALLS[tool], headers=headers) for tool in order}
        log = rows(client, "SELECT token_name, action FROM audit_log WHERE token_name = ? ORDER BY id", MCP_ACTOR)
    assert first == 1 and second == 2
    for tool in order:
        assert results[tool]["isError"] is False, (tool, results[tool])
    actions = [r["action"] for r in log]
    assert {"upload", "update", "verify", "merge", "delete"} <= set(actions), actions


def test_the_token_is_never_logged(open_app: OpenApp, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("DEBUG"):
        with open_app(models=False) as client:
            call(client, "upload", WRITE_CALLS["upload"], headers={"authorization": "Bearer secret-xyz"})
            call(client, "upload", WRITE_CALLS["upload"], token=True)
    assert "secret-xyz" not in caplog.text
    assert "testtoken" not in caplog.text


def test_upload_with_a_token_stores_the_doc_and_audits_the_token_name(open_app: OpenApp) -> None:
    with open_app() as client:
        data = ok(
            call(
                client,
                "upload",
                {"title": "Runbook", "content": EBS, "tags": ["ops"]},
                token=True,
            )
        )
        doc = client.get(f"/api/docs/{data['doc_id']}").json()
        log = audit_rows(client)
        found = ok(call(client, "search", {"query": "How do I attach an EBS volume to a Linux instance?"}))
    assert data["status"] == "created" and data["chunks"] >= 3
    assert "candidates" not in data  # same wire form as REST
    assert doc["title"] == "Runbook" and doc["tags"] == ["ops"]
    assert log == [("test", "upload", data["doc_id"])]
    assert found["results"][0]["doc_id"] == data["doc_id"]  # indexed straight away


def test_upload_formats_and_duplicates(open_app: OpenApp) -> None:
    html = "<html><head><title>Page</title></head><body><article><h1>Page</h1><p>" + "word " * 60 + "</p></article></body></html>"
    with open_app() as client:
        first = ok(call(client, "upload", {"title": "A", "content": EBS}, token=True))
        again = ok(call(client, "upload", {"title": "B", "content": EBS}, token=True))
        page = ok(call(client, "upload", {"title": "P", "content": html, "format": "html"}, token=True))
        bad = call(client, "upload", {"title": "X", "content": "x", "format": "docx"}, token=True)
    assert again == {"status": "duplicate", "doc_id": first["doc_id"], "message": again["message"]}
    assert page["status"] == "created"
    assert bad["isError"] and "bad_format" in error_text(bad)


def test_possible_duplicate_is_a_result_and_force_saves_it(open_app: OpenApp) -> None:
    with open_app(KB_DUP_THRESHOLD=0.8) as client:
        first = ok(call(client, "upload", {"title": "EBS", "content": EBS}, token=True))
        near = ok(call(client, "upload", {"title": "EBS copy", "content": EBS_EDITED}, token=True))
        count_before = scalar(client, "SELECT count(*) FROM docs")
        forced = ok(
            call(client, "upload", {"title": "EBS copy", "content": EBS_EDITED, "force": True}, token=True)
        )
    assert near["status"] == "possible_duplicate"
    assert near["candidates"][0]["doc_id"] == first["doc_id"]
    assert "force=true" in near["message"]
    assert count_before == 1
    assert forced["status"] == "created"


def test_update_verify_merge_and_delete_through_mcp(open_app: OpenApp) -> None:
    with open_app() as client:
        a = ok(call(client, "upload", {"title": "EBS", "content": EBS, "tags": ["x"]}, token=True))["doc_id"]
        b = ok(call(client, "upload", {"title": "S3", "content": S3, "tags": ["y"]}, token=True))["doc_id"]
        tags_only = ok(call(client, "update", {"doc_id": a, "tags": ["storage"]}, token=True))
        retitled = ok(call(client, "update", {"doc_id": a, "title": "EBS volumes"}, token=True))
        nothing = call(client, "update", {"doc_id": a}, token=True)
        verified = ok(call(client, "verify", {"doc_id": a, "note": "checked in console"}, token=True))
        merged = ok(call(client, "merge", {"target_id": a, "source_ids": [b]}, token=True))
        after_merge = client.get(f"/api/docs/{a}").json()
        gone = call(client, "get", {"doc_id": b})
        deleted = ok(call(client, "delete", {"doc_id": a}, token=True))
        missing = call(client, "delete", {"doc_id": a}, token=True)
        log = rows(client, "SELECT token_name, action, doc_id, note FROM audit_log ORDER BY id")
    assert tags_only["status"] == "updated" and tags_only["rechunked"] is False
    assert retitled["rechunked"] is True
    assert nothing["isError"] and "bad_request" in error_text(nothing)
    assert verified["verified_by"] == "test" and verified["status"] == "verified"
    assert merged["merged"] == [b] and merged["deleted"] == [b]
    assert sorted(after_merge["tags"]) == ["storage", "y"]
    assert gone["isError"] and "not_found" in error_text(gone)
    assert deleted == {"status": "deleted", "doc_id": a}
    assert missing["isError"] and "not_found" in error_text(missing)
    assert [(r["token_name"], r["action"]) for r in log] == [
        ("test", "upload"), ("test", "upload"), ("test", "update"), ("test", "update"),
        ("test", "verify"), ("test", "merge"), ("test", "merge_source"), ("test", "delete"),
    ]
    assert [r["note"] for r in log if r["action"] == "verify"] == ["checked in console"]


def test_merge_validates_the_source_list(open_app: OpenApp) -> None:
    with open_app(models=False) as client:
        empty = call(client, "merge", {"target_id": 1, "source_ids": []}, token=True)
    assert empty["isError"]


def test_ingest_url_fetches_stores_and_reports_blocked_addresses(
    open_app: OpenApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = ("# Guide\n\n" + "Attach the volume in the console. " * 20).encode()
    fetched: list[str] = []

    def fake_fetch(url: str, *, transport: Any = None) -> FetchResult:
        fetched.append(url)
        return FetchResult(final_url=url, content_type="text/markdown", body=body)

    monkeypatch.setattr(service, "fetch_url", fake_fetch)
    with open_app() as client:
        data = ok(call(client, "ingest_url", {"url": "https://docs.example.com/guide.md", "tags": ["aws"]}, token=True))
        doc = client.get(f"/api/docs/{data['doc_id']}").json()
        again = ok(call(client, "ingest_url", {"url": "https://docs.example.com/guide.md"}, token=True))
    assert data["status"] == "created"
    assert doc["source_url"] == "https://docs.example.com/guide.md" and doc["tags"] == ["aws"]
    assert again["status"] == "duplicate" and again["doc_id"] == data["doc_id"]
    assert fetched == ["https://docs.example.com/guide.md"] * 2


def test_ingest_url_blocks_metadata_addresses_before_any_request(open_app: OpenApp) -> None:
    with open_app(models=False) as client:
        result = call(client, "ingest_url", {"url": "http://169.254.169.254/latest/meta-data/"}, token=True)
        count = scalar(client, "SELECT count(*) FROM docs")
    assert result["isError"] and "blocked_address" in error_text(result)
    assert count == 0


def test_a_crash_shows_no_details(open_app: OpenApp, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("secret internals /data/kb.db")

    monkeypatch.setattr(service, "list_tags", boom)
    with open_app(models=False) as client:
        result = call(client, "list_tags")
    assert result["isError"]
    assert error_text(result) == "Error executing tool list_tags"


# --- mcp_actor ------------------------------------------------------------------------------


def _ctx(headers: dict[str, str] | None) -> Any:
    """Just enough of `Context` for the helper: `ctx.request_context.request.headers`."""
    request = None if headers is None else SimpleNamespace(headers=headers)
    return SimpleNamespace(request_context=SimpleNamespace(request=request))


@pytest.fixture
def tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    from kb import config

    monkeypatch.setenv("KB_WRITE_TOKENS", "test:testtoken,demo:demotoken")
    config.clear_token_cache()
    yield
    config.clear_token_cache()


def test_http_names_the_actor_from_a_valid_token_else_mcp(tokens: None) -> None:
    assert mcp_actor(_ctx({"authorization": "Bearer testtoken"})) == "test"
    assert mcp_actor(_ctx({"authorization": "bearer demotoken"})) == "demo"
    for headers in ({}, {"authorization": "Bearer nope"}, {"authorization": "Token testtoken"}):
        assert mcp_actor(_ctx(headers)) == MCP_ACTOR


def test_http_never_falls_back_to_the_environment(tokens: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_TOKEN", "testtoken")
    assert mcp_actor(_ctx(None)) == MCP_ACTOR  # a request-less call over HTTP
    assert mcp_actor(_ctx({})) == MCP_ACTOR  # a request without the header


def test_stdio_names_the_actor_from_kb_token(tokens: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_TOKEN", "demotoken")
    assert mcp_actor(_ctx(None), stdio=True) == "demo"
    monkeypatch.setenv("KB_TOKEN", "wrong")
    assert mcp_actor(_ctx(None), stdio=True) == MCP_ACTOR
    monkeypatch.delenv("KB_TOKEN")
    assert mcp_actor(_ctx(None), stdio=True) == MCP_ACTOR


def test_an_unreadable_token_store_still_lets_the_call_through(monkeypatch: pytest.MonkeyPatch) -> None:
    from kb import config

    monkeypatch.delenv("KB_WRITE_TOKENS", raising=False)
    config.clear_token_cache()

    def broken() -> dict[str, str]:
        raise RuntimeError("ssm unreachable")

    monkeypatch.setattr(config, "load_write_tokens", broken)
    assert mcp_actor(_ctx({"authorization": "Bearer anything"})) == MCP_ACTOR


def test_build_mcp_accepts_a_state_or_a_function(svc: Any) -> None:
    from kb.search.state import SearchState

    for source in (svc.state, lambda: svc.state):
        server = build_mcp(source)
        assert sorted(t.name for t in _list(server)) == sorted(TOOL_NAMES)
    assert isinstance(svc.state, SearchState)


def _list(server: Any) -> list[Any]:
    import asyncio

    return asyncio.run(server.list_tools())
