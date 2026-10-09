"""`update` with `format`: html and pdf content is converted like an upload does it."""

from __future__ import annotations

import base64
import ipaddress
import socket
from pathlib import Path

import httpx
import pytest

from kb import service
from kb.errors import ServiceError
from kb.ingest.errors import IngestError
from tests.conftest import AUTH, OpenApp, Svc
from tests.corpus import EBS
from tests.helpers import upload
from tests.mcp_helpers import call, ok
from tests.ops_helpers import audit_rows, get_doc
from tests.pdfs import make_text_pdf

FIXTURES = Path(__file__).parent / "fixtures"
HTML = (FIXTURES / "aws_like.html").read_text(encoding="utf-8")


def patch(client, doc_id: int, **body: object):  # type: ignore[no-untyped-def]
    return client.patch(f"/api/docs/{doc_id}", json=body, headers=AUTH)


def test_default_format_is_markdown(open_app: OpenApp) -> None:
    with open_app() as client:
        doc_id = upload(client, EBS)
        resp = patch(client, doc_id, content="# Amazon EBS volumes\n\n" + "A new sentence about volumes. " * 20)
        assert resp.status_code == 200 and resp.json()["rechunked"] is True


def test_html_content_is_converted_like_an_upload(open_app: OpenApp) -> None:
    with open_app() as client:
        doc_id = upload(client, EBS, ["storage"])
        before = get_doc(client, doc_id)
        resp = patch(client, doc_id, content=HTML, format="html")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "updated" and resp.json()["rechunked"] is True
        after = get_doc(client, doc_id)
        # Same conversion as an upload of that page: the cookie banner is gone, text is Markdown.
        direct = client.post("/api/docs", json={"content": HTML, "format": "html"}, headers=AUTH)
    assert direct.status_code == 409  # identical converted content is already stored (as this doc)
    assert direct.json()["doc_id"] == doc_id
    assert after["content_md"] != before["content_md"]
    assert "cookie" not in after["content_md"].lower()
    assert "<html" not in after["content_md"].lower()
    assert after["title"] == before["title"]  # the page title does not rename the doc
    assert after["tags"] == ["storage"]
    assert after["source_type"] == before["source_type"] == "markdown"


def test_pdf_content_is_base64(open_app: OpenApp) -> None:
    pdf = base64.b64encode(make_text_pdf()).decode()
    with open_app() as client:
        doc_id = upload(client, EBS)
        resp = patch(client, doc_id, content=pdf, format="pdf")
        assert resp.status_code == 200, resp.text
        assert "Elastic Load Balancing" in get_doc(client, doc_id)["content_md"]


def test_bad_base64_and_unknown_format_change_nothing(open_app: OpenApp) -> None:
    with open_app() as client:
        doc_id = upload(client, EBS)
        before = get_doc(client, doc_id)
        bad_b64 = patch(client, doc_id, content="not base64 !!", format="pdf")
        bad_fmt = patch(client, doc_id, content="text", format="docx")
        after = get_doc(client, doc_id)
    assert bad_b64.status_code == 400 and bad_b64.json()["error"] == "bad_base64"
    assert bad_fmt.status_code == 400 and bad_fmt.json()["error"] == "bad_format"
    assert after == before


def test_format_without_content_is_ignored(open_app: OpenApp) -> None:
    with open_app() as client:
        doc_id = upload(client, EBS)
        resp = patch(client, doc_id, tags=["x"], format="html")
        assert resp.status_code == 200 and resp.json()["rechunked"] is False


def test_format_is_audited_as_a_plain_update(open_app: OpenApp) -> None:
    with open_app() as client:
        doc_id = upload(client, EBS)
        patch(client, doc_id, content=HTML, format="html")
        assert audit_rows(client)[-1] == ("test", "update", doc_id)


def test_oversized_html_is_413(open_app: OpenApp, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "MAX_UPLOAD_BYTES", 100)
    with open_app() as client:
        doc_id = upload(client, "# T\n\nshort body text that fits well under the cap")
        resp = patch(client, doc_id, content=HTML, format="html")
    assert resp.status_code == 413 and resp.json()["error"] == "too_large"


def test_mcp_update_takes_format(open_app: OpenApp) -> None:
    with open_app() as client:
        doc_id = upload(client, EBS)
        result = ok(call(client, "update", {"doc_id": doc_id, "content": HTML, "format": "html"}, token=True))
        assert result["status"] == "updated" and result["rechunked"] is True
        assert "cookie" not in get_doc(client, doc_id)["content_md"].lower()


# --- service level: file bytes and update_from_url -------------------------------------------


def test_file_bytes_replace_content(svc: Svc) -> None:
    created = service.upload(svc.conn, _req(EBS), "t", search_state=svc.state)
    assert created.doc_id is not None
    result = service.update(
        svc.conn, created.doc_id, "t", format="pdf", file_bytes=make_text_pdf(), search_state=svc.state
    )
    assert result.status == "updated"
    md = svc.conn.execute("SELECT content_md FROM docs WHERE id = ?", (created.doc_id,)).fetchone()[0]
    assert "Elastic Load Balancing" in md


def test_non_utf8_markdown_bytes_are_refused(svc: Svc) -> None:
    created = service.upload(svc.conn, _req(EBS), "t", search_state=svc.state)
    assert created.doc_id is not None
    with pytest.raises(IngestError) as exc:
        service.update(svc.conn, created.doc_id, "t", file_bytes=b"\xff\xfe\x00bad", search_state=svc.state)
    assert exc.value.code == "bad_encoding"


@pytest.fixture
def fake_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(host: str, port=None, *args, **kwargs):  # type: ignore[no-untyped-def]
        try:
            ip = str(ipaddress.ip_address(host))
        except ValueError:
            ip = "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)


@pytest.mark.usefixtures("fake_dns")
def test_update_from_url_uses_the_fetched_page(svc: Svc) -> None:
    created = service.upload(svc.conn, _req(EBS), "t", search_state=svc.state)
    assert created.doc_id is not None
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=HTML.encode(), headers={"content-type": "text/html"})
    )
    result = service.update_from_url(
        svc.conn, created.doc_id, "https://docs.example.com/page.html", "t",
        search_state=svc.state, transport=transport,
    )
    assert result.status == "updated"
    md = svc.conn.execute("SELECT content_md FROM docs WHERE id = ?", (created.doc_id,)).fetchone()[0]
    assert "cookie" not in md.lower() and "EBS" in md


@pytest.mark.usefixtures("fake_dns")
def test_update_from_url_refuses_blocked_addresses(svc: Svc) -> None:
    created = service.upload(svc.conn, _req(EBS), "t", search_state=svc.state)
    assert created.doc_id is not None
    with pytest.raises(IngestError) as exc:
        service.update_from_url(svc.conn, created.doc_id, "http://169.254.169.254/latest", "t")
    assert exc.value.code == "blocked_address"


def test_update_unknown_doc_with_format_is_404(svc: Svc) -> None:
    with pytest.raises(ServiceError) as exc:
        service.update(svc.conn, 999, "t", content=HTML, format="html")
    assert exc.value.http_status == 404


def _req(md: str):  # type: ignore[no-untyped-def]
    from kb.schemas import UploadRequest

    return UploadRequest(content=md)
