"""Every PLAN.md interface row has a REST route and a service function; write routes need a token.

The table below is PLAN.md's "Interfaces" table (11 tools). The route-to-test
mapping recorded in docs/PROGRESS.md points at the tests of each row.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

from kb import service
from tests.conftest import AUTH, OpenApp
from tests.corpus import EBS, S3, VPC
from tests.helpers import upload
from tests.ops_helpers import audit_rows, get_doc, scalar

# tool -> (HTTP method, path, service function, needs a write token)
INTERFACES: dict[str, tuple[str, str, str, bool]] = {
    "search": ("GET", "/api/search", "search", False),
    "get": ("GET", "/api/docs/{doc_id}", "get", False),
    "upload": ("POST", "/api/docs", "upload", True),
    "update": ("PATCH", "/api/docs/{doc_id}", "update", True),
    "delete": ("DELETE", "/api/docs/{doc_id}", "delete", True),
    "merge": ("POST", "/api/docs/merge", "merge", True),
    "ingest_url": ("POST", "/api/ingest", "ingest_url", True),
    "list_tags": ("GET", "/api/tags", "list_tags", False),
    "verify": ("POST", "/api/docs/{doc_id}/verify", "verify", True),
    "stale": ("GET", "/api/stale", "stale", False),
    "gaps": ("GET", "/api/gaps", "gaps", False),
}


def test_the_table_has_eleven_tools() -> None:
    assert len(INTERFACES) == 11


@pytest.mark.parametrize("tool", list(INTERFACES))
def test_every_tool_has_a_route_and_a_service_function(open_app: OpenApp, tool: str) -> None:
    method, path, func_name, needs_token = INTERFACES[tool]
    with open_app() as client:
        paths = client.get("/openapi.json").json()["paths"]
        assert method.lower() in paths[path], f"{method} {path} is not a registered route"
        # Without a token the write routes answer 401; the read routes never do.
        url = path.replace("{doc_id}", "1") + ("?q=ebs" if tool == "search" else "")
        resp = client.request(method, url, json=None if method == "GET" else {})
        assert (resp.status_code == 401) is needs_token, (tool, resp.status_code)
    assert callable(getattr(service, func_name))


def test_extra_read_routes_exist(open_app: OpenApp) -> None:
    with open_app() as client:
        assert client.get("/api/audit").status_code == 200
        assert client.get("/api/chunks/1").status_code == 404  # route exists; no such chunk


def test_service_signatures_name_the_actor_not_a_token() -> None:
    for name in ("upload", "update", "delete", "merge", "ingest_url", "verify"):
        assert "actor" in inspect.signature(getattr(service, name)).parameters, name
        assert "token_name" not in inspect.signature(getattr(service, name)).parameters


# --- all write routes: 401 without a token, an audit row with one --------------------------

Setup = Callable[[TestClient], dict[str, Any]]


def _two_docs(client: TestClient) -> dict[str, Any]:
    return {"a": upload(client, VPC), "b": upload(client, S3)}


WRITE_ROUTES: list[tuple[str, str, Callable[[dict[str, Any]], tuple[str, dict[str, Any]]], str]] = [
    ("update", "PATCH", lambda ids: (f"/api/docs/{ids['a']}", {"tags": ["new"]}), "update"),
    ("merge", "POST", lambda ids: ("/api/docs/merge", {"target_id": ids["a"], "source_ids": [ids["b"]]}), "merge"),
    ("verify", "POST", lambda ids: (f"/api/docs/{ids['a']}/verify", {"note": "n"}), "verify"),
    ("upload", "POST", lambda ids: ("/api/docs", {"content": EBS}), "upload"),
    ("delete", "DELETE", lambda ids: (f"/api/docs/{ids['a']}", None), "delete"),
]


@pytest.mark.parametrize("name, method, build, action", WRITE_ROUTES, ids=[r[0] for r in WRITE_ROUTES])
def test_write_routes_reject_a_missing_or_wrong_token_and_audit_a_valid_one(
    open_app: OpenApp, name: str, method: str, build: Any, action: str
) -> None:
    with open_app() as client:
        ids = _two_docs(client)
        path, body = build(ids)
        before = audit_rows(client)
        docs_before = scalar(client, "SELECT count(*) FROM docs")
        for headers in (None, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic testtoken"}):
            resp = client.request(method, path, json=body, headers=headers)
            assert resp.status_code == 401, (headers, resp.text)
            assert resp.json()["error"] == "unauthorized"
            assert resp.headers["www-authenticate"] == "Bearer"
        # Nothing changed and nothing was audited.
        assert audit_rows(client) == before
        assert scalar(client, "SELECT count(*) FROM docs") == docs_before
        ok = client.request(method, path, json=body, headers=AUTH)
        assert ok.status_code in (200, 201), ok.text
        new = audit_rows(client)[len(before):]
    assert ("test", action, ids["a"] if action != "upload" else new[0][2]) in new
    assert all(row[0] == "test" for row in new)  # the token's name, never its secret


def test_ingest_route_needs_a_token_too(open_app: OpenApp) -> None:
    with open_app() as client:
        for headers in (None, {"Authorization": "Bearer wrong"}):
            resp = client.post("/api/ingest", json={"url": "https://docs.example.com/x"}, headers=headers)
            assert resp.status_code == 401
        assert audit_rows(client) == []


def test_a_missing_token_is_judged_before_the_body(open_app: OpenApp) -> None:
    with open_app() as client:
        doc_id = upload(client, EBS)
        assert client.patch(f"/api/docs/{doc_id}", json={"title": "x"}).status_code == 401
        assert get_doc(client, doc_id, chunks=False)["title"] == "Amazon EBS volumes"


def test_read_routes_need_no_token(open_app: OpenApp) -> None:
    with open_app() as client:
        doc_id = upload(client, EBS)
        for path in (
            f"/api/docs/{doc_id}", "/api/tags", "/api/stale", "/api/gaps", "/api/audit",
            "/api/search?q=ebs",
        ):
            assert client.get(path).status_code == 200, path


def test_two_tokens_are_told_apart_in_the_audit_log(
    open_app: OpenApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kb import config

    monkeypatch.setenv("KB_WRITE_TOKENS", "alice:tok-a,bob:tok-b")
    config.clear_token_cache()
    with open_app() as client:
        doc_id = client.post(
            "/api/docs", json={"content": EBS}, headers={"Authorization": "Bearer tok-a"}
        ).json()["doc_id"]
        client.post(f"/api/docs/{doc_id}/verify", headers={"Authorization": "Bearer tok-b"})
        client.patch(f"/api/docs/{doc_id}", json={"tags": ["x"]}, headers={"Authorization": "Bearer tok-a"})
        entries = client.get("/api/audit").json()["entries"]
        doc = get_doc(client, doc_id, chunks=False)
    assert [(e["token_name"], e["action"]) for e in entries] == [
        ("alice", "update"), ("bob", "verify"), ("alice", "upload")
    ]
    assert doc["verified_by"] == "bob"
    assert "tok-a" not in str(entries) and "tok-b" not in str(entries)
