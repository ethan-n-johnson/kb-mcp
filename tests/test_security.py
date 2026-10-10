"""Phase 16 hardening: rate limits, body size, CORS, security headers, error hygiene.

Everything runs against the real app with the fake models. The deployed system
(CloudFront, the ALB, real browsers) cannot be reached from here; those checks
are listed in docs/SECURITY_REVIEW.md as "verify on laptop".
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import logging
import socket
import threading
import time
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest
import uvicorn
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from mcp import Client

from kb import config, ratelimit
from kb.clientip import ClientIpMiddleware, client_from_forwarded
from kb.logging import JsonFormatter
from kb.main import create_app
from kb.ratelimit import RATE_LIMIT_MESSAGE
from kb.security import CSP, BodySizeLimitMiddleware, BodyTooLarge, cors_origin
from tests.conftest import AUTH, OpenApp
from tests.corpus import EBS
from tests.fakes import FakeEmbedder, FakeReranker
from tests.helpers import load_corpus, upload
from tests.mcp_helpers import call, error_text, ok
from tests.test_mcp_stdio import child_env, run_client
from tests.test_ui import XSS_DOC, assert_csp_safe, parse

EXTENSION = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
EVIL = "https://evil.example"
MB = 1024 * 1024
Q = {"q": "attach an ebs volume"}


def search_status(client: TestClient, **kwargs: Any) -> int:
    return client.get("/api/search", params=Q, **kwargs).status_code


# --- rate limit: REST ------------------------------------------------------------------------


def test_default_rate_is_30_per_minute() -> None:
    assert config.Settings().search_rate == "30/minute"
    assert config.Settings.from_env({}).search_rate == "30/minute"
    assert config.Settings.from_env({"KB_SEARCH_RATE": " 5/second "}).search_rate == "5/second"


def test_the_31st_search_in_a_minute_gets_429(open_app: OpenApp) -> None:
    with open_app() as client:
        statuses = [search_status(client) for _ in range(31)]
        resp = client.get("/api/search", params=Q)
    assert statuses[:30] == [200] * 30
    assert statuses[30] == 429
    assert resp.status_code == 429
    body = resp.json()
    assert body["error"] == "rate_limited" and body["detail"] == RATE_LIMIT_MESSAGE
    assert 1 <= int(resp.headers["retry-after"]) <= 60


def test_the_limit_comes_from_the_environment(open_app: OpenApp) -> None:
    with open_app(KB_SEARCH_RATE="3/minute") as client:
        assert [search_status(client) for _ in range(5)] == [200, 200, 200, 429, 429]


def test_limit_resets_with_the_window(open_app: OpenApp) -> None:
    with open_app(KB_SEARCH_RATE="2/second") as client:
        assert [search_status(client) for _ in range(3)] == [200, 200, 429]
        time.sleep(1.1)
        assert search_status(client) == 200


def test_reset_clears_every_count(open_app: OpenApp) -> None:
    with open_app(KB_SEARCH_RATE="1/minute") as client:
        assert search_status(client) == 200 and search_status(client) == 429
        ratelimit.reset()
        assert search_status(client) == 200


def test_invalid_requests_and_other_routes_do_not_use_the_budget(open_app: OpenApp) -> None:
    with open_app(KB_SEARCH_RATE="2/minute") as client:
        for _ in range(5):
            assert client.get("/api/search").status_code == 422  # missing q
            assert client.get("/api/tags").status_code == 200
            assert client.get("/healthz").status_code == 200
            assert client.get("/ui/search?q=").status_code == 200  # blank query: nothing searched
        assert [search_status(client) for _ in range(3)] == [200, 200, 429]


def test_a_bad_rate_stops_startup_instead_of_disabling_the_limit(open_app: OpenApp) -> None:
    for bad in ("lots", "30", "30/fortnight", "0/minute", "30/minute;5/second", "-1/minute"):
        with pytest.raises(Exception):
            with open_app(KB_SEARCH_RATE=bad):
                pass


# --- rate limit: one budget for every interface ----------------------------------------------


def test_rest_ui_and_mcp_share_one_budget(open_app: OpenApp) -> None:
    with open_app(KB_SEARCH_RATE="4/minute") as client:
        assert search_status(client) == 200
        assert client.get("/ui/search?q=ebs").status_code == 200
        assert client.get("/?q=ebs").status_code == 200
        assert ok(call(client, "search", {"query": "ebs"}))["mode"]
        # The fifth search, whichever way it comes in, is refused.
        assert search_status(client) == 429
        assert client.get("/ui/search?q=ebs").status_code == 429
        assert client.get("/?q=ebs").status_code == 429
        refused = call(client, "search", {"query": "ebs"})
        assert error_text(refused).endswith(RATE_LIMIT_MESSAGE)


def test_ui_search_fragment_gets_a_429_notice_htmx_can_show(open_app: OpenApp) -> None:
    with open_app(KB_SEARCH_RATE="1/minute") as client:
        client.get("/ui/search?q=ebs", headers={"HX-Request": "true"})
        resp = client.get("/ui/search?q=ebs", headers={"HX-Request": "true"})
        page = client.get("/?q=ebs")
    assert resp.status_code == 429 and resp.headers["content-type"].startswith("text/html")
    assert RATE_LIMIT_MESSAGE in resp.text and "<header" not in resp.text  # a fragment
    assert page.status_code == 429 and RATE_LIMIT_MESSAGE in page.text and "<header" in page.text
    assert_csp_safe(resp.text)


def test_mcp_tool_error_text_and_other_tools_stay_available(open_app: OpenApp) -> None:
    with open_app(KB_SEARCH_RATE="1/minute") as client:
        assert not call(client, "search", {"query": "ebs"})["isError"]
        refused = call(client, "search", {"query": "ebs"})
        assert error_text(refused) == f"Error executing tool search: {RATE_LIMIT_MESSAGE}"
        assert not call(client, "list_tags")["isError"]
        assert not call(client, "stale")["isError"]


def test_stdio_mcp_is_never_limited(tmp_path: Path) -> None:
    from tests.test_mcp_stdio import QUERY

    db_path = tmp_path / "kb.db"

    async def work(client: Client) -> list[bool]:
        return [(await client.call_tool("search", {"query": QUERY})).is_error for _ in range(5)]

    errors = run_client(child_env(db_path, KB_SEARCH_RATE="1/minute"), tmp_path / "err.log", work)
    assert errors == [False] * 5


def test_limiter_with_no_request_is_a_no_op() -> None:
    for _ in range(100):
        ratelimit.check_search_rate(None)


# --- rate limit: whose address is counted ----------------------------------------------------


@pytest.mark.parametrize(
    ("header", "hops", "expected"),
    [
        ("1.2.3.4, 9.9.9.9, 10.0.0.1", 2, "9.9.9.9"),  # CloudFront + ALB: viewer is 2nd from the right
        ("spoofed, 9.9.9.9, 10.0.0.1", 2, "9.9.9.9"),
        ("6.6.6.6, 1.2.3.4, 9.9.9.9, 10.0.0.1", 2, "9.9.9.9"),  # the caller's own entries are ignored
        ("9.9.9.9", 1, "9.9.9.9"),
        ("6.6.6.6, 9.9.9.9", 1, "9.9.9.9"),
        ("2001:db8::1, 10.0.0.1", 2, "2001:db8::1"),
        ("9.9.9.9", 2, None),  # shorter than the chain: do not guess
        ("", 2, None),
        ("1.2.3.4, not-an-ip, 10.0.0.1", 2, None),
        ("1.2.3.4", 0, None),  # no proxies: header is ignored
    ],
)
def test_client_address_is_counted_from_the_right(header: str, hops: int, expected: str | None) -> None:
    assert client_from_forwarded(header, hops) == expected


def test_clients_have_separate_budgets_and_the_left_most_entry_cannot_be_forged(open_app: OpenApp) -> None:
    def xff(*entries: str) -> dict[str, str]:
        return {"X-Forwarded-For": ", ".join(entries)}

    with open_app(KB_SEARCH_RATE="2/minute", KB_PROXY_HOPS=2) as client:
        # One real viewer (9.9.9.9) rotating the part it controls gets no extra budget.
        results = [search_status(client, headers=xff(f"1.1.1.{i}", "9.9.9.9", "10.0.0.1")) for i in range(4)]
        assert results == [200, 200, 429, 429]
        # A different viewer is unaffected.
        assert search_status(client, headers=xff("x", "8.8.8.8", "10.0.0.1")) == 200
        # So is the MCP tool, which sees the same address.
        refused = call(client, "search", {"query": "ebs"}, headers=xff("a", "9.9.9.9", "10.0.0.2"))
        assert refused["isError"]
        allowed = call(client, "search", {"query": "ebs"}, headers=xff("a", "7.7.7.7", "10.0.0.2"))
        assert not allowed["isError"]


def test_without_proxy_hops_the_header_is_ignored(open_app: OpenApp) -> None:
    with open_app(KB_SEARCH_RATE="2/minute") as client:
        results = [
            search_status(client, headers={"X-Forwarded-For": f"5.5.5.{i}, 6.6.6.6"}) for i in range(4)
        ]
    assert results == [200, 200, 429, 429]


def test_client_ip_middleware_sets_the_scope_client() -> None:
    seen: dict[str, Any] = {}

    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen["client"] = scope["client"]

    async def run(headers: list[tuple[bytes, bytes]], hops: int) -> Any:
        scope = {"type": "http", "client": ("10.0.0.9", 1234), "headers": headers}
        await ClientIpMiddleware(app, hops=hops)(scope, None, None)  # type: ignore[arg-type]
        return seen["client"]

    two_lines = [(b"x-forwarded-for", b"1.1.1.1, 9.9.9.9"), (b"x-forwarded-for", b"10.0.0.1")]
    assert asyncio.run(run(two_lines, 2)) == ("9.9.9.9", 0)  # repeated header lines are joined
    assert asyncio.run(run([], 2)) == ("10.0.0.9", 1234)
    assert asyncio.run(run(two_lines, 0)) == ("10.0.0.9", 1234)


# --- body size ---------------------------------------------------------------------------------


def multipart_upload(client: TestClient, size: int, headers: dict[str, str] | None = None) -> httpx.Response:
    data = b"# Big file\n\n" + b"x" * (size - 12)
    return client.post(
        "/api/docs/file", files={"file": ("big.md", data, "text/markdown")}, headers={**AUTH, **(headers or {})}
    )


BOUNDARY = "kbtestboundary"
MULTIPART = f"multipart/form-data; boundary={BOUNDARY}"


def multipart_parts(chunks: int, chunk_size: int) -> Iterator[bytes]:
    """The pieces of a multipart body holding one Markdown file of `chunks * chunk_size` bytes."""
    yield (
        f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="file"; filename="big.md"\r\n'
        "Content-Type: text/markdown\r\n\r\n# Streamed file\n\n"
    ).encode()
    for _ in range(chunks):
        yield b"x" * chunk_size
    yield f"\r\n--{BOUNDARY}--\r\n".encode()


def test_a_21_mb_upload_gets_413(open_app: OpenApp) -> None:
    with open_app() as client:
        resp = multipart_upload(client, 21 * MB)
        assert resp.status_code == 413
        assert resp.json() == {"error": "too_large", "detail": "request body exceeds the 20 MB limit"}
        assert resp.headers["connection"] == "close"
        # Refused before authentication too: an anonymous caller cannot make it read 21 MB.
        anon = client.post("/api/docs/file", files={"file": ("b.md", b"x" * (21 * MB), "text/markdown")})
        assert anon.status_code == 413
        # A normal upload still works afterwards.
        assert multipart_upload(client, 2000).status_code == 201


def test_a_21_mb_json_upload_and_mcp_call_get_413(open_app: OpenApp) -> None:
    big = {"title": "Big", "content": "y" * (21 * MB), "format": "markdown"}
    with open_app() as client:
        assert client.post("/api/docs", json=big, headers=AUTH).status_code == 413
        resp = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "upload", "arguments": big}},
            headers={"accept": "application/json, text/event-stream", **AUTH},
        )
        assert resp.status_code == 413 and resp.json()["error"] == "too_large"
        assert resp.json()["detail"] == "request body exceeds the 20 MB limit"


def test_a_chunked_21_mb_request_without_content_length_gets_413(open_app: OpenApp) -> None:
    with open_app() as client:
        request = client.build_request(
            "POST", "/api/docs/file", content=multipart_parts(21, MB), headers={**AUTH, "Content-Type": MULTIPART}
        )
        assert "content-length" not in request.headers
        assert request.headers["transfer-encoding"] == "chunked"
        resp = client.send(request)
    # The middleware's own message: the service-level check ("upload exceeds") would
    # also say 413, but only after the whole body was buffered.
    assert resp.status_code == 413 and resp.json()["detail"] == "request body exceeds the 20 MB limit"


@pytest.fixture
def live(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, int]]:
    """A real uvicorn server on a free port; yields (host, port)."""
    monkeypatch.setenv("KB_DB", str(db_path))
    monkeypatch.setenv("KB_MODELS", "on")
    monkeypatch.setenv("KB_DUP_THRESHOLD", "1.5")
    monkeypatch.setenv("KB_WRITE_TOKENS", "test:testtoken")
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
    try:
        yield "127.0.0.1", server.servers[0].sockets[0].getsockname()[1]
    finally:
        server.should_exit = True
        thread.join(timeout=20)
        config.clear_token_cache()


def raw_chunked_post(host: str, port: int, path: str, chunks: int, chunk_size: int) -> tuple[int, bytes]:
    """POST a streamed multipart upload with `Transfer-Encoding: chunked` and no Content-Length.

    Uses a raw socket so nothing computes a length for us. Returns (status, response bytes).
    """
    head = (
        f"POST {path} HTTP/1.1\r\nHost: {host}\r\nAuthorization: Bearer testtoken\r\n"
        f"Content-Type: {MULTIPART}\r\nTransfer-Encoding: chunked\r\n\r\n"
    ).encode()
    sock = socket.create_connection((host, port), timeout=30)
    try:
        sock.sendall(head)
        try:
            for piece in multipart_parts(chunks, chunk_size):
                sock.sendall(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
            sock.sendall(b"0\r\n\r\n")
        except OSError:
            pass  # the server may close as soon as it has answered
        data = b""
        try:
            while True:
                piece = sock.recv(65536)
                if not piece:
                    break
                data += piece
                if b"\r\n\r\n" in data and (b"too_large" in data or b'"doc_id"' in data):
                    break
        except OSError:
            pass
    finally:
        sock.close()
    status = int(data.split(b" ", 2)[1]) if data.startswith(b"HTTP/") else 0
    return status, data


def test_chunked_without_content_length_is_refused_over_a_real_socket(live: tuple[str, int]) -> None:
    host, port = live
    status, data = raw_chunked_post(host, port, "/api/docs/file", chunks=21, chunk_size=MB)
    assert status == 413, data[:300]
    assert b"request body exceeds the 20 MB limit" in data


def test_a_chunked_request_under_the_limit_is_still_served(live: tuple[str, int]) -> None:
    host, port = live
    status, data = raw_chunked_post(host, port, "/api/docs/file", chunks=2, chunk_size=1000)
    assert status == 201, data[:300]
    # Just under the limit still passes (the file is 19 MB plus the multipart framing).
    status, data = raw_chunked_post(host, port, "/api/docs/file", chunks=19, chunk_size=MB)
    assert status in (201, 422) and b"too_large" not in data, data[:300]


class Echo:
    """ASGI app that reads the whole body and answers its length."""

    def __init__(self, swallow: bool = False) -> None:
        self.swallow = swallow

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        total = 0
        try:
            while True:
                message = await receive()
                total += len(message.get("body", b""))
                if not message.get("more_body"):
                    break
        except BodyTooLarge:
            if not self.swallow:
                raise
            await send({"type": "http.response.start", "status": 400, "headers": []})
            await send({"type": "http.response.body", "body": b"parse error"})
            return
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": str(total).encode()})


def asgi_post(app: Any, headers: list[tuple[bytes, bytes]], chunks: list[bytes]) -> tuple[int, bytes, dict[bytes, bytes]]:
    sent: list[dict[str, Any]] = []
    queue = [
        {"type": "http.request", "body": c, "more_body": i < len(chunks) - 1} for i, c in enumerate(chunks)
    ] or [{"type": "http.request", "body": b"", "more_body": False}]

    async def receive() -> dict[str, Any]:
        return queue.pop(0)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/", "headers": headers}
    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start["status"], body, dict(start["headers"])


def test_the_limit_is_exact_for_declared_and_chunked_bodies() -> None:
    app = BodySizeLimitMiddleware(Echo(), max_bytes=1000)
    at = [b"a" * 400, b"b" * 400, b"c" * 200]
    over = [b"a" * 400, b"b" * 400, b"c" * 201]
    assert asgi_post(app, [], at)[:2] == (200, b"1000")
    assert asgi_post(app, [(b"content-length", b"1000")], at)[:2] == (200, b"1000")
    assert asgi_post(app, [], over)[0] == 413  # chunked, no Content-Length
    assert asgi_post(app, [(b"content-length", b"1001")], over)[0] == 413
    # A lying Content-Length does not help: the bytes are counted anyway.
    assert asgi_post(app, [(b"content-length", b"10")], over)[0] == 413


def test_a_413_replaces_an_error_the_app_made_of_the_failed_read() -> None:
    """FastAPI turns a failed body read into a 400; the client must still see the 413."""
    app = BodySizeLimitMiddleware(Echo(swallow=True), max_bytes=100)
    status, body, _ = asgi_post(app, [], [b"x" * 60, b"x" * 60])
    assert status == 413 and json.loads(body)["error"] == "too_large"


@pytest.mark.parametrize("value", [b"abc", b"-5", b"1e3", b""])
def test_a_malformed_content_length_is_a_400(value: bytes) -> None:
    status, body, _ = asgi_post(BodySizeLimitMiddleware(Echo()), [(b"content-length", value)], [b"x"])
    assert status == 400 and json.loads(body)["error"] == "bad_request"


def test_upload_limit_keeps_the_pdf_page_limit_and_service_limit() -> None:
    from kb import service

    assert service.MAX_UPLOAD_BYTES == 20 * MB
    assert BodySizeLimitMiddleware(Echo()).max_bytes == 20 * MB


# --- CORS --------------------------------------------------------------------------------------


def preflight(client: TestClient, origin: str, method: str = "PATCH", headers: str = "authorization,content-type") -> httpx.Response:
    return client.options(
        "/api/docs/1",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": headers,
        },
    )


def cors_headers(resp: httpx.Response) -> dict[str, str]:
    return {k: v for k, v in resp.headers.items() if k.startswith("access-control-")}


def test_preflight_from_the_extension_succeeds(open_app: OpenApp) -> None:
    with open_app(KB_EXTENSION_ORIGIN=EXTENSION) as client:
        resp = preflight(client, EXTENSION)
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == EXTENSION
    methods = {m.strip() for m in resp.headers["access-control-allow-methods"].split(",")}
    assert methods == {"GET", "POST", "PATCH", "DELETE"}
    allowed = {h.strip().lower() for h in resp.headers["access-control-allow-headers"].split(",")}
    # Starlette also lists the CORS-safelisted request headers; nothing custom is allowed.
    assert {"authorization", "content-type"} <= allowed <= {"authorization", "content-type", "accept", "accept-language", "content-language"}
    assert "access-control-allow-credentials" not in resp.headers


def test_preflight_from_another_site_gets_no_allow_origin(open_app: OpenApp) -> None:
    with open_app(KB_EXTENSION_ORIGIN=EXTENSION) as client:
        for origin in (EVIL, "chrome-extension://zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz", "null", "http://localhost:8000"):
            resp = preflight(client, origin)
            assert "access-control-allow-origin" not in resp.headers, origin
            assert resp.status_code == 400
        # An allowed origin asking for a method or header outside the list is refused as well.
        assert preflight(client, EXTENSION, method="PUT").status_code == 400
        assert preflight(client, EXTENSION, headers="x-custom").status_code == 400


def test_real_responses_only_carry_cors_headers_for_the_extension(open_app: OpenApp) -> None:
    with open_app(KB_EXTENSION_ORIGIN=EXTENSION) as client:
        mine = client.get("/api/tags", headers={"Origin": EXTENSION})
        evil = client.get("/api/tags", headers={"Origin": EVIL})
        none = client.get("/api/tags")
        too_big = multipart_upload(client, 21 * MB, headers={"Origin": EXTENSION})
    assert mine.headers["access-control-allow-origin"] == EXTENSION
    assert cors_headers(evil) == {} and cors_headers(none) == {}
    assert evil.status_code == 200  # CORS stops a page reading the answer, it does not refuse the request
    assert "*" not in {v for r in (mine, evil, none) for v in cors_headers(r).values()}
    # Errors are readable by the extension too, including the middleware's own 413.
    assert too_big.status_code == 413 and too_big.headers["access-control-allow-origin"] == EXTENSION


def test_without_an_extension_origin_no_cors_header_is_ever_sent(open_app: OpenApp) -> None:
    with open_app() as client:
        assert client.app.state.settings.extension_origin is None
        responses = [
            preflight(client, EXTENSION),
            preflight(client, EVIL),
            client.get("/api/tags", headers={"Origin": EXTENSION}),
            client.get("/", headers={"Origin": EXTENSION}),
            client.post("/api/docs", json={"content": "x"}, headers={"Origin": EXTENSION}),
            multipart_upload(client, 21 * MB, headers={"Origin": EXTENSION}),
        ]
    for resp in responses:
        assert cors_headers(resp) == {}, resp.request.url
    assert responses[0].status_code in (400, 405)  # nothing answers the preflight


@pytest.mark.parametrize(
    "value", ["*", "null", EXTENSION + "/", "https://a.example, https://b.example", "chrome-extension://*", "evil.example", " "]
)
def test_a_bad_origin_setting_turns_cors_off_instead_of_opening_it(open_app: OpenApp, value: str) -> None:
    assert cors_origin(value) is None
    with open_app(KB_EXTENSION_ORIGIN=value) as client:
        resp = preflight(client, EVIL)
        assert cors_headers(resp) == {}
        assert cors_headers(client.get("/api/tags", headers={"Origin": value})) == {}


def test_cors_origin_accepts_one_origin_only() -> None:
    assert cors_origin(EXTENSION) == EXTENSION
    assert cors_origin("http://localhost:8000") == "http://localhost:8000"
    assert cors_origin(None) is None and cors_origin("") is None


# --- security headers and the UI under the policy ----------------------------------------------

EXPECTED_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' https: data:; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
)


def test_csp_is_the_one_in_the_phase_file() -> None:
    assert CSP == EXPECTED_CSP


def test_html_responses_carry_csp_nosniff_and_referrer_policy(open_app: OpenApp) -> None:
    with open_app() as client:
        doc_id = upload(client, EBS, ["storage"])
        pages = [
            "/", "/?q=ebs", "/add", "/maintenance", f"/docs/{doc_id}", "/docs/9999", "/ui/search?q=ebs",
            "/ui/search?q=",
        ]
        responses = {path: client.get(path) for path in pages}
        fragment = client.get("/docs/9999", headers={"HX-Request": "true"})
    for path, resp in {**responses, "fragment": fragment}.items():
        assert resp.headers["content-type"].startswith("text/html"), path
        assert resp.headers["content-security-policy"] == EXPECTED_CSP, path
        assert resp.headers["x-content-type-options"] == "nosniff", path
        assert resp.headers["referrer-policy"] == "no-referrer", path


def test_json_and_static_responses_get_nosniff_but_no_csp(open_app: OpenApp) -> None:
    with open_app() as client:
        for path in ("/api/tags", "/healthz", "/openapi.json", "/static/app.js", "/static/app.css"):
            resp = client.get(path)
            assert resp.status_code == 200, path
            assert resp.headers["x-content-type-options"] == "nosniff", path
            assert "content-security-policy" not in resp.headers, path
        assert client.get("/api/docs/999").headers["x-content-type-options"] == "nosniff"


def test_swagger_ui_is_off_because_it_needs_a_cdn_script(open_app: OpenApp) -> None:
    with open_app() as client:
        for path in ("/docs", "/redoc"):
            assert client.get(path).status_code == 404
        assert client.get("/openapi.json").status_code == 200


def test_every_template_and_script_is_compatible_with_script_src_self() -> None:
    """Static scan of the sources: nothing that `script-src 'self'` or `style-src 'self'` would block."""
    root = Path(__file__).parent.parent / "kb"
    for path in sorted((root / "templates").glob("*.html")):
        for tag, attrs in parse(path.read_text(encoding="utf-8")):
            assert tag != "style", f"{path.name}: <style>"
            if tag == "script":
                assert attrs.get("src", "").startswith("/static/"), f"{path.name}: inline or external script"
            if tag == "link":
                assert (attrs.get("href") or "").startswith("/static/"), f"{path.name}: external stylesheet"
            for name, value in attrs.items():
                assert name != "style", f"{path.name}: style attribute on <{tag}>"
                assert not name.startswith(("on", "hx-on")), f"{path.name}: {name} on <{tag}>"
                assert name not in ("srcdoc", "formaction") and "javascript:" not in (value or "").lower()
    js = (root / "static" / "app.js").read_text(encoding="utf-8")
    for banned in ("eval(", "Function(", "document.write", "insertAdjacentHTML", ".innerHTML", "setTimeout(\"", "setAttribute(\"on"):
        assert banned not in js, banned
    css = (root / "static" / "app.css").read_text(encoding="utf-8")
    assert "@import" not in css and "url(" not in css.replace("url(data:", "")  # no external loads


def test_htmx_is_configured_not_to_eval_or_inject_styles(open_app: OpenApp) -> None:
    """htmx would inject a <style> for its indicators and can `eval`; the page config turns both off."""
    with open_app() as client:
        html = client.get("/").text
        htmx = client.get("/static/htmx-2.0.4.min.js").text
    meta = next(a for t, a in parse(html) if t == "meta" and a.get("name") == "htmx-config")
    cfg = json.loads(meta["content"] or "{}")
    assert cfg["allowEval"] is False and cfg["includeIndicatorStyles"] is False
    assert cfg["allowScriptTags"] is False and cfg["selfRequestsOnly"] is True
    # Pin the two guards in the vendored build: every eval path goes through the allowEval check,
    # and the indicator style is only inserted when includeIndicatorStyles is not false.
    assert "function vn(e,t,n){if(Q.config.allowEval){return t()}" in htmx
    assert "if(Q.config.includeIndicatorStyles!==false){" in htmx
    assert htmx.count("eval(") == 1 and "vn(ne().body,function(){return eval(e)})" in htmx


def test_pages_with_hostile_content_are_still_inert_under_the_policy(open_app: OpenApp) -> None:
    hostile_title = "<img src=x onerror=alert(1)> \"><script>alert(2)</script>"
    with open_app() as client:
        resp = client.post(
            "/api/docs",
            json={"title": hostile_title, "content": XSS_DOC.replace("# XSS probe", "## XSS probe"), "format": "markdown",
                  "tags": ["\"><svg onload=alert(3)>"], "source_url": "https://example.com/a?b=\"onmouseover=\"alert(4)"},
            headers=AUTH,
        )
        assert resp.status_code == 201, resp.text
        doc_id = resp.json()["doc_id"]
        client.post(f"/api/docs/{doc_id}/verify", json={"note": "<script>alert(5)</script>"}, headers=AUTH)
        pages = ["/?q=probe", "/ui/search?q=probe", f"/docs/{doc_id}", "/maintenance?days=0"]
        for path in pages:
            resp = client.get(path)
            assert resp.status_code == 200, path
            assert_csp_safe(resp.text)
            for tag, attrs in parse(resp.text):
                assert tag not in ("svg", "iframe", "object", "embed"), (path, tag)
                if tag == "img":
                    assert not any(n.startswith("on") for n in attrs), (path, attrs)
        doc_html = client.get(f"/docs/{doc_id}").text
        assert hostile_title not in doc_html  # never raw
        assert html.unescape(doc_html.split("<h1>", 1)[1].split("</h1>", 1)[0]) == hostile_title  # shown as text


# --- write routes need a token (enumerated, so a new route cannot be forgotten) -----------------


def api_routes() -> list[APIRoute]:
    """Every route of the REST and UI routers, found through the routers so new routes are included."""
    found: dict[tuple[str, str], APIRoute] = {}

    def walk(routes: Any) -> None:
        for route in routes:
            if isinstance(route, APIRoute):
                for method in route.methods:
                    found[(method, route.path)] = route
            elif hasattr(route, "original_router"):
                walk(route.original_router.routes)

    from kb import api, ui, ui_add

    for module in (api, ui, ui_add):
        walk(module.router.routes)
    return list(found.values())


TICKET_ROUTES = {"/api/uploads/{ticket}"}
"""Write routes that take a one-time upload ticket instead of a bearer token."""


def test_every_non_get_route_refuses_a_missing_or_wrong_token(open_app: OpenApp) -> None:
    with open_app() as client:
        checked = []
        for route in api_routes():
            if route.path in TICKET_ROUTES:
                # The ticket in the path is the permission (kb/uploads.py): no bearer
                # token, and a made-up ticket is refused before anything is read.
                for method in sorted(route.methods - {"GET", "HEAD", "OPTIONS"}):
                    resp = client.request(method, route.path.replace("{ticket}", "guess"), content=b"x")
                    assert resp.status_code == 404 and resp.json()["error"] == "bad_ticket"
                continue
            for method in sorted(route.methods - {"GET", "HEAD", "OPTIONS"}):
                path = route.path.replace("{doc_id}", "1")
                for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic dGVzdDp0ZXN0dG9rZW4="}):
                    # The UI routes take form posts; send plausible fields so validation passes
                    # and only the token decides.
                    body = (
                        {"data": {"on": "true", "content": "x", "url": "https://example.com/", "title": "t"}}
                        if path.startswith("/ui/") else {"json": {}}
                    )
                    resp = client.request(method, path, headers=headers, **body)
                    assert resp.status_code == 401, (method, path, headers, resp.status_code)
                    if path.startswith("/api/"):
                        assert resp.headers.get("www-authenticate") == "Bearer", (method, path)
                checked.append((method, route.path))
        assert len(checked) >= 12, checked  # the REST write routes and the /ui/ write routes
        assert {"/ui/add/paste", "/ui/admin/fallback"} <= {p for _, p in checked}


def test_every_mcp_write_tool_refuses_a_missing_or_wrong_token(open_app: OpenApp) -> None:
    arguments = {
        "upload": {"title": "t", "content": "c"}, "upload_file": {"filename": "a.pdf"},
        "update": {"doc_id": 1, "title": "x"},
        "delete": {"doc_id": 1}, "merge": {"target_id": 1, "source_ids": [2]},
        "ingest_url": {"url": "https://example.com/"}, "verify": {"doc_id": 1},
    }
    with open_app() as client:
        for tool, args in arguments.items():
            for headers in ({}, {"Authorization": "Bearer wrong"}):
                result = call(client, tool, args, headers=headers)
                assert error_text(result).endswith("This tool needs a valid write token"), tool


# --- error hygiene -------------------------------------------------------------------------------


class Captured(logging.Handler):
    """Collects every record as the JSON line the real formatter would write."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []
        self.setFormatter(JsonFormatter())

    # Test clients log their own requests (even headers at DEBUG); only the server's logs count.
    CLIENT_LOGGERS = ("httpx", "httpx2", "httpcore", "asyncio", "anyio")

    def emit(self, record: logging.LogRecord) -> None:
        if not record.name.startswith(self.CLIENT_LOGGERS):
            self.lines.append(self.format(record))


@pytest.fixture
def captured_logs() -> Iterator[Captured]:
    handler = Captured()
    root = logging.getLogger()
    root.addHandler(handler)
    previous = root.level
    root.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)


SECRET_IN_MESSAGE = "s3cr3t-value-in-an-exception-message"


@contextlib.contextmanager
def boom_app(open_app: OpenApp) -> Iterator[TestClient]:
    """A running app with two routes that raise; the client answers 500 instead of re-raising."""
    with open_app() as client:

        @client.app.get("/api/boom")
        def boom() -> None:
            raise RuntimeError(SECRET_IN_MESSAGE)

        @client.app.get("/ui-boom")
        async def ui_boom() -> None:
            raise ValueError(SECRET_IN_MESSAGE)

        yield TestClient(client.app, raise_server_exceptions=False)


def test_unhandled_exceptions_give_a_bare_500_without_a_stack_trace(open_app: OpenApp) -> None:
    with boom_app(open_app) as client:
        for path in ("/api/boom", "/ui-boom"):
            resp = client.get(path, headers={"Authorization": "Bearer testtoken"})
            assert resp.status_code == 500, path
            assert resp.json() == {"error": "internal", "detail": ""}
            for leak in ("Traceback", "RuntimeError", "ValueError", SECRET_IN_MESSAGE, ".py", "File \""):
                assert leak not in resp.text, (path, leak)
        # Still bare after a failed call: the app keeps serving.
        assert client.get("/healthz").status_code == 200


def test_logs_never_contain_the_authorization_header_or_a_token(open_app: OpenApp, captured_logs: Captured) -> None:
    token_line = {"Authorization": "Bearer testtoken", "Cookie": "session=cookie-secret-1"}
    wrong = {"Authorization": "Bearer wrongtoken-abcdefghijklmnopqrstuvwxyz"}
    with boom_app(open_app) as client:
        doc_id = upload(client, EBS, ["storage"])
        client.post(f"/api/docs/{doc_id}/verify", json={"note": "checked"}, headers=token_line)
        client.patch(f"/api/docs/{doc_id}", json={"title": "Renamed"}, headers=token_line)
        client.post("/api/docs", json={"content": "x"}, headers=wrong)
        client.get("/api/boom", headers=token_line)  # the 500 path logs a traceback
        client.post("/ui/admin/fallback", data={"on": "true"}, headers=token_line)
        call(client, "upload", {"title": "n", "content": "# n\n\nbody text here."}, token=True)
        call(client, "delete", {"doc_id": doc_id}, headers=wrong)
        client.delete(f"/api/docs/{doc_id}", headers=token_line)
    text = "\n".join(captured_logs.lines)
    assert text, "nothing was logged"
    assert "unhandled error" in text  # the 500 path really was exercised
    for secret in ("testtoken", "wrongtoken", "cookie-secret-1", "Bearer", "authorization"):
        assert secret.lower() not in text.lower(), secret


def test_logs_from_a_real_server_never_contain_the_token(live: tuple[str, int], captured_logs: Captured) -> None:
    """uvicorn's own access log runs here (the TestClient bypasses it)."""
    host, port = live
    base = f"http://{host}:{port}"
    headers = {"Authorization": "Bearer testtoken"}
    with httpx.Client(base_url=base, timeout=20) as client:
        client.post("/api/docs", json={"content": EBS}, headers=headers)
        client.post("/api/docs", json={"content": EBS}, headers={"Authorization": "Bearer nope-nope-nope"})
        client.get("/api/search", params=Q, headers=headers)
    text = "\n".join(captured_logs.lines)
    assert "uvicorn" in text or "kb." in text
    assert "testtoken" not in text and "nope-nope-nope" not in text and "Bearer" not in text


# --- the review's SSRF and XSS items (also run as part of docs/SECURITY_REVIEW.md) -----------------

BLOCKED = [
    "http://169.254.169.254/latest/meta-data/",
    "http://169.254.169.254/latest/api/token",
    "http://10.0.11.10/",
    "http://10.0.0.1:8000/healthz",
    "http://127.0.0.1/", "http://localhost:8000/", "http://0.0.0.0/", "http://[::1]/",
    "http://[::ffff:169.254.169.254]/", "http://[::ffff:a9fe:a9fe]/", "http://[fd00:ec2::254]/",
    "http://2852039166/",  # 169.254.169.254 as one decimal number
    "http://0xa9fea9fe/", "http://0251.0376.0251.0376/", "http://169.254.43518/", "http://127.1/",
    "http://user:pass@169.254.169.254/",
    "http://172.17.0.1/", "http://192.168.1.1/", "http://100.64.0.1/",
]


@pytest.mark.parametrize("url", BLOCKED)
def test_ingest_url_refuses_internal_addresses_on_every_interface(open_app: OpenApp, url: str) -> None:
    with open_app() as client:
        resp = client.post("/api/ingest", json={"url": url}, headers=AUTH)
        assert resp.status_code == 400 and resp.json()["error"] == "blocked_address", (url, resp.text)
        result = call(client, "ingest_url", {"url": url}, token=True)
        assert "blocked_address" in error_text(result), url
        ui = client.post("/ui/add/url", data={"url": url}, headers={**AUTH, "HX-Request": "true"})
        assert ui.status_code == 400 and "blocked_address" in ui.text, url
        assert client.get("/api/stale?days=0").json()["docs"] == []  # nothing was stored


def test_a_redirect_into_the_metadata_service_is_refused_through_ingest_url(
    open_app: OpenApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket as sock

    from kb.ingest import fetch

    real = sock.getaddrinfo
    monkeypatch.setattr(
        fetch.socket, "getaddrinfo",
        lambda host, *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))] if host == "public.example" else real(host, *a, **k),
    )
    hops: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hops.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data/iam/security-credentials/"})

    transport = httpx.MockTransport(handler)
    real_fetch = fetch.fetch_url
    monkeypatch.setattr("kb.service.fetch_url", lambda url, **kw: real_fetch(url, transport=transport))
    with open_app() as client:
        resp = client.post("/api/ingest", json={"url": "http://public.example/start"}, headers=AUTH)
    assert resp.status_code == 400 and resp.json()["error"] == "blocked_address"
    assert hops == ["http://public.example/start"]


def test_load_corpus_is_unaffected_by_the_hardening(open_app: OpenApp) -> None:
    """Smoke test: a normal session (uploads, searches, UI) works with every middleware installed."""
    with open_app(KB_EXTENSION_ORIGIN=EXTENSION, KB_PROXY_HOPS=2) as client:
        ids = load_corpus(client)
        assert len(ids) >= 3
        assert search_status(client, headers={"X-Forwarded-For": "1.1.1.1, 9.9.9.9, 10.0.0.1"}) == 200
        assert client.get("/", headers={"Origin": EXTENSION}).status_code == 200


def test_the_image_does_not_trust_a_forwarded_for_header_it_cannot_verify() -> None:
    """uvicorn with `--forwarded-allow-ips '*'` takes the caller-controlled left-most entry."""
    dockerfile = (Path(__file__).parent.parent / "Dockerfile").read_text(encoding="utf-8")
    cmd = next(line for line in dockerfile.splitlines() if line.startswith("CMD"))
    assert "--no-proxy-headers" in cmd and "--forwarded-allow-ips" not in cmd
    assert '"--workers", "1"' in cmd


def test_the_image_makes_the_code_readable_for_the_runtime_user() -> None:
    """COPY keeps the build machine's modes; owner-only files broke the boot (seen on the first sandbox boot, 2026-10-09)."""
    dockerfile = (Path(__file__).parent.parent / "Dockerfile").read_text(encoding="utf-8")
    flat = dockerfile.replace("\\\n", " ")
    lines = flat.splitlines()
    user = next(i for i, line in enumerate(lines) if line.startswith("USER 10001"))
    chmod = next(i for i, line in enumerate(lines) if "chmod -R a+rX /app" in line)
    assert chmod < user and 'purelib"])\')/kb"' in lines[chmod]
    # The first command run as the runtime user imports the app, so unreadable code fails the build.
    after = "\n".join(lines[user:])
    assert "import kb.cli, kb.main;" in after.split("from fastembed", 1)[0]
