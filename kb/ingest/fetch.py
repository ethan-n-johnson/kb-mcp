"""httpx fetch with SSRF guard and size limits."""

from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from kb.ingest.errors import IngestError

__all__ = ["FetchResult", "IngestError", "check_url", "fetch_url"]

MAX_BYTES = 20 * 1024 * 1024
MAX_REDIRECTS = 5
TIMEOUT_SECONDS = 15.0
USER_AGENT = "kb-mcp/0.1 (course knowledge base; single-page fetch)"
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

_IP = ipaddress.IPv4Address | ipaddress.IPv6Address


@dataclass(frozen=True)
class FetchResult:
    """A fetched page: the URL after redirects, its media type, and the body."""

    final_url: str
    content_type: str
    body: bytes


def _is_forbidden(ip: _IP) -> bool:
    """True when `ip` is not an ordinary public unicast address."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    )


def check_url(url: str) -> None:
    """Refuse `url` unless it is http(s) and every resolved address is public.

    Raises `IngestError` with code `bad_scheme`, `blocked_address` or
    `fetch_failed` (unparseable URL or failed DNS lookup). This runs before any
    network connection is opened, and again for every redirect hop.

    Known limitation: this checks the addresses and the HTTP client then
    resolves the host again to connect, so a DNS-rebinding attacker could
    answer differently the second time. Closing that gap would mean pinning the
    connection to the checked address; it is deliberately not solved here.
    """
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise IngestError("fetch_failed", f"invalid URL: {exc}") from exc
    if parts.scheme.lower() not in ("http", "https"):
        raise IngestError("bad_scheme", "only http and https URLs are allowed")
    if not host:
        raise IngestError("fetch_failed", "URL has no host")
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise IngestError("fetch_failed", f"could not resolve host {host!r}") from exc
    if not infos:
        raise IngestError("fetch_failed", f"could not resolve host {host!r}")
    for info in infos:
        raw = str(info[4][0]).split("%", 1)[0]  # drop an IPv6 zone id
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError as exc:
            raise IngestError("blocked_address", f"unparseable address for {host!r}") from exc
        if _is_forbidden(ip):
            raise IngestError(
                "blocked_address", f"{host!r} resolves to a non-public address"
            )


def _media_type(header: str | None) -> str:
    """Lowercased media type of a Content-Type header, parameters removed."""
    return (header or "").split(";", 1)[0].strip().lower()


def _read_body(response: httpx.Response, deadline: float) -> bytes:
    """Stream the response body, aborting past `MAX_BYTES` or the deadline."""
    declared = response.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BYTES:
        raise IngestError("too_large", "response is larger than 20 MB")
    chunks: list[bytes] = []
    total = 0
    for piece in response.iter_bytes():
        total += len(piece)
        if total > MAX_BYTES:
            raise IngestError("too_large", "response is larger than 20 MB")
        if time.monotonic() > deadline:
            raise IngestError("fetch_failed", "download timed out")
        chunks.append(piece)
    return b"".join(chunks)


def fetch_url(url: str, *, transport: httpx.BaseTransport | None = None) -> FetchResult:
    """Fetch one page with SSRF protection.

    Only http and https are allowed. Redirects are followed manually (at most 5)
    and each hop gets the full address check. The body is streamed and aborted
    past 20 MB; the whole call is bounded by a 15 second timeout. `transport`
    exists so tests can inject `httpx.MockTransport`.

    Raises `IngestError` (`bad_scheme`, `blocked_address`, `too_large`,
    `fetch_failed`) for every refusal.
    """
    deadline = time.monotonic() + TIMEOUT_SECONDS
    current = url
    try:
        with httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(TIMEOUT_SECONDS),
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
        ) as client:
            for _hop in range(MAX_REDIRECTS + 1):
                check_url(current)
                with client.stream("GET", current) as response:
                    if response.status_code in _REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            raise IngestError(
                                "fetch_failed", "redirect without a Location header"
                            )
                        current = urljoin(current, location)
                        continue
                    if response.status_code >= 400:
                        raise IngestError(
                            "fetch_failed", f"server returned HTTP {response.status_code}"
                        )
                    body = _read_body(response, deadline)
                    return FetchResult(
                        final_url=current,
                        content_type=_media_type(response.headers.get("content-type")),
                        body=body,
                    )
    except httpx.HTTPError as exc:
        raise IngestError("fetch_failed", f"request failed: {type(exc).__name__}") from exc
    raise IngestError("fetch_failed", f"more than {MAX_REDIRECTS} redirects")
