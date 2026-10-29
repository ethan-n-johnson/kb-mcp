"""Upload, get and delete through the service and REST, with S3 under moto."""

from __future__ import annotations

import base64
from pathlib import Path

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from kb import service
from kb.errors import ServiceError
from kb.ingest.errors import IngestError
from kb.schemas import UploadRequest
from kb.store import Store
from tests.helpers import fts_count, fts_integrity_check, reopen
from tests.pdfs import make_image_only_pdf, make_text_pdf

pytestmark = pytest.mark.dual

FIXTURES = Path(__file__).parent / "fixtures"
AUTH = {"Authorization": "Bearer testtoken"}
BUCKET = "kb-test-bucket"

MD = """---
title: Load balancing notes
tags: [elb, networking]
---
# Load balancing notes

An Application Load Balancer distributes incoming traffic across targets in
multiple Availability Zones and performs health checks on every target.

## Health checks

A target group health check sends periodic requests to each target, and
unhealthy targets stop receiving traffic until they pass again.
"""


def _post_md(client: TestClient, content: str = MD, **extra: object) -> object:
    body = {"content": content, "format": "markdown", **extra}
    return client.post("/api/docs", json=body, headers=AUTH)


def _count(conn: Store, table: str) -> int:
    return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


# --- upload and get -----------------------------------------------------------


def test_upload_markdown_then_get_with_tags_and_chunks(client: TestClient) -> None:
    resp = _post_md(client, tags=["course", " elb "])
    assert resp.status_code == 201
    created = resp.json()
    assert created["status"] == "created"
    assert created["chunks"] >= 1

    doc = client.get(f"/api/docs/{created['doc_id']}", params={"include_chunks": "true"}).json()
    assert doc["title"] == "Load balancing notes"
    assert doc["source_type"] == "markdown"
    assert doc["tags"] == ["course", "elb", "networking"]  # request tags + front matter, sorted
    assert "health check" in doc["content_md"].lower()
    assert doc["content_md"].startswith("# Load balancing notes")  # front matter stripped
    assert len(doc["chunks"]) == doc["chunk_count"] == created["chunks"]
    assert [c["ord"] for c in doc["chunks"]] == list(range(len(doc["chunks"])))
    assert doc["created_at"] == doc["updated_at"]
    assert doc["verified_at"] is None


def test_get_omits_chunks_by_default(client: TestClient) -> None:
    doc_id = _post_md(client).json()["doc_id"]
    assert client.get(f"/api/docs/{doc_id}").json()["chunks"] is None


def test_request_title_overrides_derived_title(client: TestClient) -> None:
    doc_id = _post_md(client, title="  My title  ").json()["doc_id"]
    assert client.get(f"/api/docs/{doc_id}").json()["title"] == "My title"


def test_chunk_route_returns_parent_doc_with_chunk_marked(client: TestClient) -> None:
    doc_id = _post_md(client).json()["doc_id"]
    chunks = client.get(f"/api/docs/{doc_id}", params={"include_chunks": True}).json()["chunks"]
    target = chunks[-1]["id"]
    doc = client.get(f"/api/chunks/{target}").json()
    assert doc["id"] == doc_id
    assert [c["id"] for c in doc["chunks"] if c["matched"]] == [target]
    assert client.get("/api/chunks/99999").status_code == 404


def test_service_get_needs_exactly_one_id(conn: Store) -> None:
    for kwargs in ({}, {"doc_id": 1, "chunk_id": 1}):
        with pytest.raises(ServiceError) as err:
            service.get(conn, **kwargs)
        assert err.value.http_status == 400


def test_source_url_is_stored_and_must_be_http(client: TestClient) -> None:
    ok = _post_md(client, source_url="https://docs.example/ebs.html").json()
    assert client.get(f"/api/docs/{ok['doc_id']}").json()["source_url"] == "https://docs.example/ebs.html"
    bad = _post_md(client, content=MD + "\nmore", source_url="javascript:alert(1)")
    assert bad.status_code == 400
    assert bad.json()["error"] == "bad_source_url"


# --- duplicates -----------------------------------------------------------------


def test_same_content_is_a_duplicate_and_writes_nothing(client: TestClient, conn: Store) -> None:
    first = _post_md(client).json()
    audit_before = _count(conn, "audit_log")
    again = _post_md(client)
    assert again.status_code == 409
    body = again.json()
    assert body["error"] == "duplicate"
    assert body["status"] == "duplicate"
    assert body["doc_id"] == first["doc_id"]
    assert _count(conn, "docs") == 1
    assert _count(conn, "audit_log") == audit_before


def test_whitespace_only_differences_are_duplicates(client: TestClient) -> None:
    first = _post_md(client).json()
    noisy = MD.replace("\n\n", "\n\n\n\n").replace("target group", "target   group") + "   \n\n"
    again = _post_md(client, content=noisy)
    assert again.status_code == 409
    assert again.json()["doc_id"] == first["doc_id"]


def test_different_content_is_not_a_duplicate(client: TestClient, conn: Store) -> None:
    _post_md(client)
    assert _post_md(client, content=MD + "\nOne more sentence of text.").status_code == 201
    assert _count(conn, "docs") == 2


def test_commit_rechecks_the_hash_inside_the_transaction(conn: Store) -> None:
    prepared = service.prepare_upload(UploadRequest(content=MD))
    first = service.commit_prepared(conn, prepared, "t")
    second = service.commit_prepared(conn, prepared, "t")
    assert first[1] is True and second == (first[0], False)
    assert _count(conn, "docs") == 1 and _count(conn, "audit_log") == 1


# --- prepare / commit split and hooks -------------------------------------------


def test_prepare_upload_touches_no_database() -> None:
    prepared = service.prepare_upload(UploadRequest(content=MD, tags=["x"]))
    assert prepared.title == "Load balancing notes"
    assert prepared.tags == ["x", "elb", "networking"]
    assert len(prepared.content_hash) == 64
    assert prepared.chunks and prepared.original is None


def test_hooks_run_after_commit_in_order(
    conn: Store, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[tuple[str, int, int]] = []

    def written(c: Store, doc_id: int, search_state: object = None) -> None:
        # A second connection sees the rows only if the transaction has committed.
        other = reopen(db_path)
        try:
            events.append(("written", doc_id, _count(other, "chunks")))
        finally:
            other.close()

    monkeypatch.setattr(service, "_after_chunks_written", written)
    monkeypatch.setattr(
        service, "_after_doc_deleted", lambda doc_id, st=None: events.append(("deleted", doc_id, 0))
    )
    result = service.upload(conn, UploadRequest(content=MD), "t")
    assert result.doc_id is not None
    service.delete(conn, result.doc_id, "t")
    assert events == [("written", result.doc_id, result.chunks), ("deleted", result.doc_id, 0)]


def test_hooks_do_not_run_for_duplicates_or_missing_docs(
    conn: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    service.upload(conn, UploadRequest(content=MD), "t")
    called: list[str] = []
    monkeypatch.setattr(service, "_after_chunks_written", lambda c, d, st=None: called.append("w"))
    monkeypatch.setattr(service, "_after_doc_deleted", lambda d, st=None: called.append("d"))
    assert service.upload(conn, UploadRequest(content=MD), "t").status == "duplicate"
    with pytest.raises(ServiceError):
        service.delete(conn, 999, "t")
    assert called == []


# --- originals in S3 ----------------------------------------------------------------


def _bucket(monkeypatch: pytest.MonkeyPatch) -> object:
    monkeypatch.setenv("KB_BUCKET", BUCKET)
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket=BUCKET)
    return s3


def test_html_upload_saves_original_and_s3_key(
    client: TestClient, conn: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    html = (FIXTURES / "aws_like.html").read_text(encoding="utf-8")
    with mock_aws():
        s3 = _bucket(monkeypatch)
        resp = client.post("/api/docs", json={"content": html, "format": "html"}, headers=AUTH)
        assert resp.status_code == 201, resp.text
        doc_id = resp.json()["doc_id"]
        key = f"originals/{doc_id}/original.html"
        stored = conn.execute("SELECT s3_key, source_type FROM docs WHERE id = ?", (doc_id,)).fetchone()
        assert stored["s3_key"] == key and stored["source_type"] == "html"
        obj = s3.get_object(Bucket=BUCKET, Key=key)
        assert obj["Body"].read() == html.encode("utf-8")
        assert obj["ContentType"] == "text/html"
        assert obj["ServerSideEncryption"] == "AES256"


def test_multipart_pdf_original_keeps_its_filename(
    client: TestClient, conn: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = make_text_pdf()
    with mock_aws():
        s3 = _bucket(monkeypatch)
        resp = client.post(
            "/api/docs/file",
            files={"file": ("../my guide (v2).pdf", pdf, "application/pdf")},
            headers=AUTH,
        )
        assert resp.status_code == 201, resp.text
        doc_id = resp.json()["doc_id"]
        key = conn.execute("SELECT s3_key FROM docs WHERE id = ?", (doc_id,)).fetchone()["s3_key"]
        assert key == f"originals/{doc_id}/my_guide_v2_.pdf"
        assert s3.get_object(Bucket=BUCKET, Key=key)["Body"].read() == pdf


def test_markdown_is_not_saved_to_s3(
    client: TestClient, conn: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    with mock_aws():
        s3 = _bucket(monkeypatch)
        doc_id = _post_md(client).json()["doc_id"]
        assert "Contents" not in s3.list_objects_v2(Bucket=BUCKET)
        assert conn.execute("SELECT s3_key FROM docs WHERE id = ?", (doc_id,)).fetchone()[0] is None


def test_no_bucket_means_no_aws_call(
    client: TestClient, conn: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("boto3.client must not be called when KB_BUCKET is unset")

    monkeypatch.setattr(boto3, "client", boom)
    html = (FIXTURES / "aws_like.html").read_text(encoding="utf-8")
    resp = client.post("/api/docs", json={"content": html, "format": "html"}, headers=AUTH)
    assert resp.status_code == 201
    assert conn.execute("SELECT s3_key FROM docs").fetchone()[0] is None


def test_s3_failure_keeps_the_upload(
    client: TestClient, conn: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    html = (FIXTURES / "aws_like.html").read_text(encoding="utf-8")
    with mock_aws():
        monkeypatch.setenv("KB_BUCKET", "bucket-that-does-not-exist")
        resp = client.post("/api/docs", json={"content": html, "format": "html"}, headers=AUTH)
    assert resp.status_code == 201
    assert conn.execute("SELECT s3_key FROM docs").fetchone()[0] is None


# --- files and PDFs ---------------------------------------------------------------------


def test_multipart_markdown_with_title_tags_and_force(client: TestClient) -> None:
    resp = client.post(
        "/api/docs/file",
        files={"file": ("notes.md", MD.encode(), "text/markdown")},
        data={"title": "Uploaded notes", "tags": "a, b,,a", "force": "true"},
        headers=AUTH,
    )
    assert resp.status_code == 201
    doc = client.get(f"/api/docs/{resp.json()['doc_id']}").json()
    assert doc["title"] == "Uploaded notes"
    assert doc["tags"] == ["a", "b", "elb", "networking"]


def test_multipart_pdf_upload_works(client: TestClient) -> None:
    resp = client.post(
        "/api/docs/file",
        files={"file": ("guide.pdf", make_text_pdf(), "application/pdf")},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    doc = client.get(f"/api/docs/{resp.json()['doc_id']}", params={"include_chunks": True}).json()
    assert doc["source_type"] == "pdf"
    assert any("Health checks" in c["heading_path"] for c in doc["chunks"])


def test_image_only_pdf_is_422_no_text_layer(client: TestClient, conn: Store) -> None:
    resp = client.post(
        "/api/docs/file",
        files={"file": ("scan.pdf", make_image_only_pdf(), "application/pdf")},
        headers=AUTH,
    )
    assert resp.status_code == 422
    assert resp.json()["error"] == "no_text_layer"
    assert _count(conn, "docs") == 0


def test_json_pdf_is_base64(client: TestClient) -> None:
    body = {"content": base64.b64encode(make_text_pdf()).decode(), "format": "pdf", "title": "PDF doc"}
    assert client.post("/api/docs", json=body, headers=AUTH).status_code == 201
    bad = client.post("/api/docs", json={**body, "content": "not base64!!"}, headers=AUTH)
    assert bad.status_code == 400 and bad.json()["error"] == "bad_base64"


# --- delete ------------------------------------------------------------------------------


def test_delete_removes_doc_chunks_tags_and_fts_and_audits(
    client: TestClient, conn: Store
) -> None:
    doc_id = _post_md(client, tags=["course"]).json()["doc_id"]
    assert fts_count(conn, "balancer") > 0

    resp = client.delete(f"/api/docs/{doc_id}", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json() == {"status": "deleted", "doc_id": doc_id}

    assert _count(conn, "docs") == _count(conn, "chunks") == _count(conn, "doc_tags") == 0
    assert fts_count(conn, "balancer") == 0
    fts_integrity_check(conn)  # raises if the SQLite FTS5 index is stale
    assert client.get(f"/api/docs/{doc_id}").status_code == 404

    rows = conn.execute("SELECT token_name, action, doc_id FROM audit_log ORDER BY id").fetchall()
    assert [(r["token_name"], r["action"], r["doc_id"]) for r in rows] == [
        ("test", "upload", doc_id),
        ("test", "delete", doc_id),
    ]


def test_deleting_a_missing_doc_is_404_and_unaudited(client: TestClient, conn: Store) -> None:
    resp = client.delete("/api/docs/42", headers=AUTH)
    assert resp.status_code == 404 and resp.json()["error"] == "not_found"
    assert _count(conn, "audit_log") == 0


def test_the_same_content_can_be_uploaded_again_after_delete(client: TestClient) -> None:
    first = _post_md(client).json()["doc_id"]
    client.delete(f"/api/docs/{first}", headers=AUTH)
    assert _post_md(client).status_code == 201


# --- errors --------------------------------------------------------------------------------


def test_error_responses(client: TestClient) -> None:
    unknown = _post_md(client, format="docx")
    assert unknown.status_code == 400 and unknown.json()["error"] == "bad_format"

    empty = _post_md(client, content="   \n\n")
    assert empty.status_code == 422 and empty.json()["error"] == "empty_content"

    invalid = client.post("/api/docs", json={"format": "markdown"}, headers=AUTH)
    assert invalid.status_code == 422
    assert invalid.json()["error"] == "invalid_request" and "content" in invalid.json()["detail"]

    missing = client.get("/api/nope")
    assert missing.status_code == 404 and missing.json()["error"] == "not_found"


def test_too_large_upload_is_413(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "MAX_UPLOAD_BYTES", 100)
    big = _post_md(client, content="word " * 100)
    assert big.status_code == 413 and big.json()["error"] == "too_large"
    big_file = client.post(
        "/api/docs/file", files={"file": ("a.md", b"word " * 100, "text/markdown")}, headers=AUTH
    )
    assert big_file.status_code == 413


def test_unhandled_errors_are_a_bare_500(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(service, "get", explode)
    quiet = TestClient(client.app, raise_server_exceptions=False)
    resp = quiet.get("/api/docs/1")
    assert resp.status_code == 500
    assert resp.json() == {"error": "internal", "detail": ""}
    assert "secret" not in resp.text and "Traceback" not in resp.text


def test_service_raises_typed_errors(conn: Store) -> None:
    with pytest.raises(IngestError) as err:
        service.upload(conn, UploadRequest(content=" ", format="markdown"), "t")
    assert err.value.code == "empty_content"
