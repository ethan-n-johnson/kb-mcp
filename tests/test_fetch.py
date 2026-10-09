"""SSRF guard and size limits of `fetch_url`; no real network is used."""

from __future__ import annotations

import ipaddress
import socket
from typing import Callable, Iterator

import httpx
import pytest

from kb.ingest import fetch as fetch_mod
from kb.ingest.fetch import IngestError, fetch_url

PUBLIC_IP = "93.184.216.34"


def _addrinfo(ip: str) -> tuple:
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    sockaddr = (ip, 0, 0, 0) if ":" in ip else (ip, 0)
    return (family, socket.SOCK_STREAM, 6, "", sockaddr)


@pytest.fixture
def resolve(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Install a fake `getaddrinfo`; unknown hosts resolve to a public address."""
    table: dict[str, list[str]] = {}

    def fake(host: str, port=None, *args, **kwargs):
        try:
            ipaddress.ip_address(host)  # IP literals resolve to themselves
        except ValueError:
            return [_addrinfo(ip) for ip in table.get(host, [PUBLIC_IP])]
        return [_addrinfo(host)]

    monkeypatch.setattr(socket, "getaddrinfo", fake)

    def register(host: str, *ips: str) -> None:
        table[host] = list(ips)

    return register


def _must_not_connect(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"network request attempted: {request.url}")


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.0.0.5",
        "169.254.169.254",
        "::1",
        "::ffff:127.0.0.1",
        "::ffff:169.254.169.254",
        "192.168.1.20",
        "172.16.0.1",
        "0.0.0.0",
        "224.0.0.1",
        "100.64.0.1",
        "fe80::1%eth0",
        "fc00::1",
    ],
)
def test_private_addresses_are_refused_before_any_request(resolve, ip: str) -> None:
    resolve("target.example", ip)
    with pytest.raises(IngestError) as err:
        fetch_url("http://target.example/page", transport=httpx.MockTransport(_must_not_connect))
    assert err.value.code == "blocked_address"


def test_any_private_address_among_several_blocks(resolve) -> None:
    resolve("mixed.example", PUBLIC_IP, "10.0.0.5")
    with pytest.raises(IngestError) as err:
        fetch_url("https://mixed.example/", transport=httpx.MockTransport(_must_not_connect))
    assert err.value.code == "blocked_address"


def test_metadata_ip_literal_is_blocked_without_dns_mocking() -> None:
    with pytest.raises(IngestError) as err:
        fetch_url("http://169.254.169.254/latest/meta-data/")
    assert err.value.code == "blocked_address"


@pytest.mark.parametrize(
    "url", ["http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://127.0.0.1:8000/", "http://localhost/"]
)
def test_loopback_literals_are_blocked(url: str) -> None:
    with pytest.raises(IngestError) as err:
        fetch_url(url)
    assert err.value.code == "blocked_address"


@pytest.mark.parametrize(
    "url", ["ftp://example.com/file", "file:///etc/passwd", "gopher://example.com/", "javascript:alert(1)", "example.com/page"]
)
def test_only_http_and_https(resolve, url: str) -> None:
    with pytest.raises(IngestError) as err:
        fetch_url(url, transport=httpx.MockTransport(_must_not_connect))
    assert err.value.code == "bad_scheme"


def test_unresolvable_host_is_fetch_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args, **kwargs):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    with pytest.raises(IngestError) as err:
        fetch_url("http://nope.example/", transport=httpx.MockTransport(_must_not_connect))
    assert err.value.code == "fetch_failed"


def test_redirect_to_private_host_is_refused(resolve) -> None:
    resolve("public.example", PUBLIC_IP)
    resolve("internal.example", "10.0.0.5")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://internal.example/secret"})

    with pytest.raises(IngestError) as err:
        fetch_url("http://public.example/start", transport=httpx.MockTransport(handler))
    assert err.value.code == "blocked_address"
    assert seen == ["http://public.example/start"]  # the private hop was never requested


def test_redirect_to_metadata_literal_is_refused(resolve) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"Location": "http://169.254.169.254/latest/meta-data/"})

    with pytest.raises(IngestError) as err:
        fetch_url("https://public.example/", transport=httpx.MockTransport(handler))
    assert err.value.code == "blocked_address"


def test_redirect_to_other_scheme_is_refused(resolve) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "file:///etc/passwd"})

    with pytest.raises(IngestError) as err:
        fetch_url("https://public.example/", transport=httpx.MockTransport(handler))
    assert err.value.code == "bad_scheme"


def test_relative_redirect_is_followed_and_final_url_reported(resolve) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(301, headers={"Location": "/new/page.html"})
        return httpx.Response(200, headers={"Content-Type": "text/html; charset=utf-8"}, content=b"<p>hi</p>")

    result = fetch_url("https://public.example/old", transport=httpx.MockTransport(handler))
    assert result.final_url == "https://public.example/new/page.html"
    assert result.content_type == "text/html"
    assert result.body == b"<p>hi</p>"


def test_too_many_redirects(resolve) -> None:
    count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(302, headers={"Location": f"/hop{count}"})

    with pytest.raises(IngestError) as err:
        fetch_url("https://public.example/", transport=httpx.MockTransport(handler))
    assert err.value.code == "fetch_failed"
    assert count == 6  # the original request plus 5 redirects


def test_five_redirects_are_allowed(resolve) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        hop = int(request.url.path.removeprefix("/hop") or 0) if request.url.path != "/" else 0
        if hop < 5:
            return httpx.Response(302, headers={"Location": f"/hop{hop + 1}"})
        return httpx.Response(200, content=b"done")

    assert fetch_url("https://public.example/", transport=httpx.MockTransport(handler)).body == b"done"


def test_body_over_limit_is_refused_while_streaming(resolve, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fetch_mod, "MAX_BYTES", 5 * 1024)
    produced = 0

    def stream() -> Iterator[bytes]:
        nonlocal produced
        for _ in range(100):
            produced += 1
            yield b"x" * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=stream())

    with pytest.raises(IngestError) as err:
        fetch_url("https://public.example/big", transport=httpx.MockTransport(handler))
    assert err.value.code == "too_large"
    assert produced < 100  # aborted early rather than reading everything


def test_real_20_mb_limit(resolve) -> None:
    limit = 20 * 1024 * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (limit + 1))

    with pytest.raises(IngestError) as err:
        fetch_url("https://public.example/big", transport=httpx.MockTransport(handler))
    assert err.value.code == "too_large"


def test_exactly_20_mb_is_accepted(resolve) -> None:
    limit = 20 * 1024 * 1024

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * limit)

    assert len(fetch_url("https://public.example/", transport=httpx.MockTransport(handler)).body) == limit


def test_declared_content_length_over_limit_is_refused(resolve) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Length": str(50 * 1024 * 1024)}, content=b"tiny")

    with pytest.raises(IngestError) as err:
        fetch_url("https://public.example/", transport=httpx.MockTransport(handler))
    assert err.value.code == "too_large"


def test_http_error_status_is_fetch_failed(resolve) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"missing")

    with pytest.raises(IngestError) as err:
        fetch_url("https://public.example/gone", transport=httpx.MockTransport(handler))
    assert err.value.code == "fetch_failed"


def test_transport_error_is_fetch_failed(resolve) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(IngestError) as err:
        fetch_url("https://public.example/", transport=httpx.MockTransport(handler))
    assert err.value.code == "fetch_failed"


def test_sends_descriptive_user_agent(resolve) -> None:
    agents: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        agents.append(request.headers["user-agent"])
        return httpx.Response(200, content=b"ok")

    fetch_url("https://public.example/", transport=httpx.MockTransport(handler))
    assert agents and agents[0].startswith("kb-mcp/")
