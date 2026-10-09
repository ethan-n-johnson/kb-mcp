"""`ingest_url`: fetch, detect the format, then the upload path. No real network.

`fetch_url` takes `transport=`, which `service.ingest_url` passes through for
these tests; it is not part of the REST API. DNS is faked so the SSRF guard
runs for real against the mock hosts.
"""

from __future__ import annotations

import ipaddress
import socket
from pathlib import Path
from typing import Callable

import boto3
import httpx
import pytest
from moto import mock_aws

from kb import service
from kb.errors import ServiceError
from kb.ingest.errors import IngestError
from kb.ingest.fetch import fetch_url
from kb.schemas import IngestRequest
from tests.conftest import AUTH, OpenApp, Svc
from tests.corpus import EBS, S3
from tests.ops_helpers import EBS_EDITED, audit_rows, get_doc, scalar
from tests.pdfs import make_text_pdf

FIXTURES = Path(__file__).parent / "fixtures"
HTML = (FIXTURES / "aws_like.html").read_bytes()
URL = "https://docs.example.com/ec2/latest/ug/ebs-attaching-volume.html"
PUBLIC_IP = "93.184.216.34"
BUCKET = "kb-ingest-bucket"

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every host resolves to a public address (IP literals to themselves); records lookups."""
    looked_up: list[str] = []

    def fake(host: str, port=None, *args, **kwargs):  # type: ignore[no-untyped-def]
        looked_up.append(host)
        try:
            ip = str(ipaddress.ip_address(host))
        except ValueError:
            ip = PUBLIC_IP
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 6, "", (ip, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return looked_up


def page(body: bytes = HTML, content_type: str = "text/html; charset=utf-8") -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda request: httpx.Response(200, content=body, headers={"content-type": content_type})
    )


def test_public_html_page_is_fetched_converted_and_stored(svc: Svc) -> None:
    result = service.ingest_url(
        svc.conn, URL, "tester", tags=["ec2", " docs "], search_state=svc.state, transport=page()
    )
    assert result.status == "created" and result.doc_id is not None and result.chunks
    row = svc.conn.execute("SELECT * FROM docs WHERE id = ?", (result.doc_id,)).fetchone()
    assert row["source_url"] == URL and row["source_type"] == "html"
    assert row["title"].startswith("Attach an Amazon EBS volume")
    assert "cookie" not in row["content_md"].lower()  # trafilatura dropped the banner
    tags = [r["tag"] for r in svc.conn.execute("SELECT tag FROM doc_tags ORDER BY tag")]
    assert tags == ["docs", "ec2"]
    # It went through the normal write path: vectors stored and indexed, audited.
    assert svc.conn.execute("SELECT count(*) FROM chunks WHERE embedding IS NULL").fetchone()[0] == 0
    assert len(svc.state.index) == result.chunks
    assert svc.conn.execute("SELECT token_name, action FROM audit_log").fetchone()[:] == ("tester", "upload")


def test_source_url_is_the_final_url_after_redirects(svc: Svc) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(301, headers={"location": "/ec2/latest/ug/ebs-attaching-volume.html"})
        return httpx.Response(200, content=HTML, headers={"content-type": "text/html"})

    result = service.ingest_url(
        svc.conn, "https://docs.example.com/old", "t", search_state=svc.state,
        transport=httpx.MockTransport(handler),
    )
    stored = svc.conn.execute("SELECT source_url FROM docs WHERE id = ?", (result.doc_id,)).fetchone()
    assert stored["source_url"] == URL


def test_markdown_and_pdf_responses_are_detected_from_the_response(svc: Svc) -> None:
    md = service.ingest_url(
        svc.conn, "https://docs.example.com/notes", "t", search_state=svc.state,
        transport=page(EBS.encode(), "text/markdown; charset=utf-8"),
    )
    pdf = service.ingest_url(
        svc.conn, "https://docs.example.com/guide", "t", search_state=svc.state,
        transport=page(make_text_pdf(), "application/pdf"),
    )
    types = {
        r["id"]: r["source_type"] for r in svc.conn.execute("SELECT id, source_type FROM docs")
    }
    assert types[md.doc_id] == "markdown" and types[pdf.doc_id] == "pdf"


def test_html_and_pdf_originals_go_to_s3(svc: Svc, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf = make_text_pdf()
    with mock_aws():
        monkeypatch.setenv("KB_BUCKET", BUCKET)
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)
        html_doc = service.ingest_url(
            svc.conn, URL, "t", search_state=svc.state, transport=page()
        )
        pdf_doc = service.ingest_url(
            svc.conn, "https://docs.example.com/guide.pdf", "t", search_state=svc.state,
            transport=page(pdf, "application/pdf"),
        )
        md_doc = service.ingest_url(
            svc.conn, "https://docs.example.com/notes.md", "t", search_state=svc.state,
            transport=page(S3.encode(), "text/markdown"),
        )
        keys = {
            r["id"]: r["s3_key"] for r in svc.conn.execute("SELECT id, s3_key FROM docs")
        }
        assert keys[html_doc.doc_id] == f"originals/{html_doc.doc_id}/original.html"
        assert keys[pdf_doc.doc_id] == f"originals/{pdf_doc.doc_id}/original.pdf"
        assert keys[md_doc.doc_id] is None  # Markdown is its own source of truth
        assert s3.get_object(Bucket=BUCKET, Key=keys[html_doc.doc_id])["Body"].read() == HTML
        assert s3.get_object(Bucket=BUCKET, Key=keys[pdf_doc.doc_id])["Body"].read() == pdf


def test_same_page_twice_is_an_exact_duplicate(svc: Svc) -> None:
    first = service.ingest_url(svc.conn, URL, "t", search_state=svc.state, transport=page())
    second = service.ingest_url(svc.conn, URL, "t", search_state=svc.state, transport=page())
    assert second.status == "duplicate" and second.doc_id == first.doc_id


def test_near_duplicate_page_returns_candidates_and_force_saves_it(svc: Svc) -> None:
    original = service.ingest_url(
        svc.conn, "https://docs.example.com/ebs.md", "t", search_state=svc.state,
        transport=page(EBS.encode(), "text/markdown"),
    )
    edited = page(EBS_EDITED.encode(), "text/markdown")
    flagged = service.ingest_url(
        svc.conn, "https://mirror.example.net/ebs.md", "t", search_state=svc.state, transport=edited
    )
    assert flagged.status == "possible_duplicate"
    assert flagged.candidates[0].doc_id == original.doc_id
    assert flagged.candidates[0].source_url == "https://docs.example.com/ebs.md"
    forced = service.ingest_url(
        svc.conn, "https://mirror.example.net/ebs.md", "t", force=True, search_state=svc.state,
        transport=edited,
    )
    assert forced.status == "created"


def test_without_models_the_check_is_skipped(svc: Svc) -> None:
    result = service.ingest_url(svc.conn, URL, "t", transport=page())  # no search_state
    assert result.status == "created"
    assert result.near_duplicate_check == "skipped: models unavailable"


@pytest.mark.parametrize(
    "url, code",
    [
        ("http://169.254.169.254/latest/meta-data/", "blocked_address"),
        ("http://127.0.0.1:8000/admin", "blocked_address"),
        ("file:///etc/passwd", "bad_scheme"),
        ("ftp://docs.example.com/x", "bad_scheme"),
    ],
)
def test_ssrf_guard_applies_before_any_request(svc: Svc, url: str, code: str) -> None:
    def never(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"network request attempted: {request.url}")

    with pytest.raises(IngestError) as err:
        service.ingest_url(
            svc.conn, url, "t", search_state=svc.state, transport=httpx.MockTransport(never)
        )
    assert err.value.code == code
    assert svc.conn.execute("SELECT count(*) FROM docs").fetchone()[0] == 0


def test_http_errors_and_unreadable_pages_store_nothing(svc: Svc) -> None:
    with pytest.raises(IngestError) as err:
        service.ingest_url(
            svc.conn, URL, "t", transport=httpx.MockTransport(lambda r: httpx.Response(503))
        )
    assert err.value.code == "fetch_failed"
    with pytest.raises(IngestError) as empty:
        service.ingest_url(svc.conn, URL, "t", transport=page(b"<html><body></body></html>"))
    assert empty.value.code == "empty_content"
    assert svc.conn.execute("SELECT count(*) FROM docs").fetchone()[0] == 0
    assert svc.conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 0


def test_over_long_final_url_is_a_clean_400(svc: Svc) -> None:
    long_url = "https://docs.example.com/" + "a" * 2100
    with pytest.raises(ServiceError) as err:
        service.ingest_url(svc.conn, long_url, "t", transport=page())
    assert err.value.code == "bad_source_url" and err.value.http_status == 400


# --- REST: the transport is not part of the API ---------------------------------------


def test_the_rest_request_has_no_transport_field() -> None:
    assert set(IngestRequest.model_fields) == {"url", "tags", "force"}


@pytest.fixture
def rest_fetch(monkeypatch: pytest.MonkeyPatch) -> Callable[[httpx.MockTransport], None]:
    """Make `POST /api/ingest` fetch through a mock transport (tests only)."""

    def install(transport: httpx.MockTransport) -> None:
        monkeypatch.setattr(
            service, "fetch_url", lambda url, *, transport=None, _t=transport: fetch_url(url, transport=_t)
        )

    return install


def test_post_ingest_stores_the_page(open_app: OpenApp, rest_fetch) -> None:
    rest_fetch(page())
    with open_app() as client:
        resp = client.post(
            "/api/ingest", json={"url": URL, "tags": ["ec2"]}, headers=AUTH
        )
        assert resp.status_code == 201, resp.text
        doc = get_doc(client, resp.json()["doc_id"], chunks=False)
        assert doc["source_url"] == URL and doc["tags"] == ["ec2"] and doc["source_type"] == "html"
        assert audit_rows(client) == [("test", "upload", doc["id"])]
        again = client.post("/api/ingest", json={"url": URL}, headers=AUTH)
    assert again.status_code == 409 and again.json()["error"] == "duplicate"
    assert again.json()["doc_id"] == doc["id"]


def test_post_ingest_possible_duplicate_is_a_200(open_app: OpenApp, rest_fetch) -> None:
    with open_app(KB_DUP_THRESHOLD=0.8) as client:
        rest_fetch(page(EBS.encode(), "text/markdown"))
        first = client.post("/api/ingest", json={"url": "https://a.example.com/ebs.md"}, headers=AUTH)
        rest_fetch(page(EBS_EDITED.encode(), "text/markdown"))
        near = client.post("/api/ingest", json={"url": "https://b.example.com/ebs.md"}, headers=AUTH)
        forced = client.post(
            "/api/ingest", json={"url": "https://b.example.com/ebs.md", "force": True}, headers=AUTH
        )
        total = scalar(client, "SELECT count(*) FROM docs")
    assert near.status_code == 200
    assert near.json()["status"] == "possible_duplicate"
    assert near.json()["candidates"][0]["doc_id"] == first.json()["doc_id"]
    assert forced.status_code == 201 and total == 2


def test_post_ingest_error_statuses(open_app: OpenApp, rest_fetch) -> None:
    with open_app() as client:
        assert client.post("/api/ingest", json={"url": URL}).status_code == 401
        blocked = client.post(
            "/api/ingest", json={"url": "http://169.254.169.254/latest/"}, headers=AUTH
        )
        scheme = client.post("/api/ingest", json={"url": "file:///etc/passwd"}, headers=AUTH)
        rest_fetch(httpx.MockTransport(lambda r: httpx.Response(404)))
        missing = client.post("/api/ingest", json={"url": URL}, headers=AUTH)
        bad_body = client.post("/api/ingest", json={}, headers=AUTH)
        total = scalar(client, "SELECT count(*) FROM audit_log")
    assert (blocked.status_code, blocked.json()["error"]) == (400, "blocked_address")
    assert (scheme.status_code, scheme.json()["error"]) == (400, "bad_scheme")
    assert (missing.status_code, missing.json()["error"]) == (502, "fetch_failed")
    assert bad_body.status_code == 422
    assert total == 0
