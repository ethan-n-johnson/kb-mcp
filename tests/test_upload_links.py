"""One-time upload links: the `upload_file` tool, `POST /api/upload-links`, `/api/uploads/{ticket}`.

The flow a chat client uses for a whole file: ask for a link (token checked),
send the bytes there (no token, the ticket is the permission), read the usual
upload result. Plus the stdio variant, where `filename` is a local path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from kb import db, uploads
from kb.uploads import TicketStore
from tests.conftest import AUTH, OpenApp
from tests.corpus import EBS
from tests.mcp_helpers import call, error_text, ok
from tests.pdfs import make_text_pdf
from tests.test_mcp_stdio import child_env, run_client


@pytest.fixture(autouse=True)
def _fresh_store() -> Any:
    uploads.STORE.clear()
    yield
    uploads.STORE.clear()


def _path(url: str) -> str:
    return urlsplit(url).path


def _rows(client: Any) -> list[Any]:
    conn = db.connect(client.app.state.settings.db_path)
    try:
        return conn.execute("SELECT * FROM docs ORDER BY id").fetchall()
    finally:
        conn.close()


# --- the ticket store ----------------------------------------------------------------------


def test_a_ticket_works_once() -> None:
    store = TicketStore()
    ticket, ttl = store.issue("alice", "a.pdf", title="T", tags=["x"])
    assert ttl == uploads.TICKET_TTL_SECONDS and len(ticket) >= 40
    got = store.take(ticket)
    assert got is not None and (got.actor, got.filename, got.title, got.tags) == ("alice", "a.pdf", "T", ("x",))
    assert store.take(ticket) is None
    assert store.take("made-up") is None


def test_a_ticket_expires() -> None:
    now = [1000.0]
    store = TicketStore(ttl=60, clock=lambda: now[0])
    ticket, _ = store.issue("alice", "a.md")
    now[0] += 61
    assert store.take(ticket) is None
    assert store.pending() == 0


def test_pending_tickets_are_capped_oldest_first() -> None:
    store = TicketStore(max_pending=3)
    first = store.issue("a", "1.md")[0]
    others = [store.issue("a", f"{i}.md")[0] for i in range(3)]
    assert store.pending() == 3
    assert store.take(first) is None
    assert all(store.take(t) is not None for t in others)


# --- over MCP (HTTP) ---------------------------------------------------------------------


def test_upload_file_works_without_a_token(open_app: OpenApp) -> None:
    with open_app(models=False) as client:
        link = ok(call(client, "upload_file", {"filename": "guide.pdf"}))
    assert link["status"] == "upload_link"
    assert uploads.STORE.pending() == 1


def test_link_then_bytes_stores_a_pdf_as_the_token_owner(open_app: OpenApp) -> None:
    pdf = make_text_pdf()
    with open_app(models=False) as client:
        link = ok(
            call(
                client, "upload_file",
                {"filename": "/home/me/Downloads/SAA Guide.pdf", "title": "Exam guide", "tags": ["saa-c03"]},
                token=True,
            )
        )
        assert link["status"] == "upload_link" and link["method"] == "POST"
        assert link["url"].startswith("http://testserver/api/uploads/")
        assert link["filename"] == "SAA_Guide.pdf"  # basename only, made safe
        assert link["expires_in"] == 900 and '--data-binary @"<path to SAA_Guide.pdf>"' in link["curl"]
        resp = client.post(_path(link["url"]), content=pdf, headers={"content-type": "application/pdf"})
        assert resp.status_code == 201, resp.text
        assert resp.json()["status"] == "created"
        again = client.post(_path(link["url"]), content=pdf)
        rows = _rows(client)
        conn = db.connect(client.app.state.settings.db_path)
        try:
            tags = [r["tag"] for r in conn.execute("SELECT tag FROM doc_tags")]
            actor = conn.execute("SELECT token_name FROM audit_log WHERE action = 'upload'").fetchone()[0]
        finally:
            conn.close()
    assert again.status_code == 404 and again.json()["error"] == "bad_ticket"
    assert len(rows) == 1 and rows[0]["title"] == "Exam guide" and rows[0]["source_type"] == "pdf"
    assert tags == ["saa-c03"] and actor == "test"


def test_put_works_too_and_markdown_is_detected_from_the_name(open_app: OpenApp) -> None:
    with open_app(models=False) as client:
        link = ok(call(client, "upload_file", {"filename": "notes.md"}, token=True))
        resp = client.put(_path(link["url"]), content=EBS.encode())
        rows = _rows(client)
    assert resp.status_code == 201
    assert rows[0]["source_type"] == "markdown" and rows[0]["title"] == "Amazon EBS volumes"


def test_kb_public_url_is_used_for_the_link(open_app: OpenApp) -> None:
    with open_app(models=False, KB_PUBLIC_URL="https://d123.cloudfront.net/") as client:
        link = ok(call(client, "upload_file", {"filename": "a.pdf"}, token=True))
    assert link["url"].startswith("https://d123.cloudfront.net/api/uploads/")


def test_unknown_extension_is_refused_before_a_ticket_exists(open_app: OpenApp) -> None:
    with open_app(models=False) as client:
        result = call(client, "upload_file", {"filename": "photo.png"}, token=True)
    assert "bad_format" in error_text(result)
    assert uploads.STORE.pending() == 0


def test_same_bytes_twice_is_a_duplicate_through_the_link(open_app: OpenApp) -> None:
    with open_app(models=False) as client:
        for expected in (201, 409):
            link = ok(call(client, "upload_file", {"filename": "notes.md"}, token=True))
            resp = client.post(_path(link["url"]), content=EBS.encode())
            assert resp.status_code == expected


def test_empty_body_and_unknown_ticket_are_errors(open_app: OpenApp) -> None:
    with open_app(models=False) as client:
        link = ok(call(client, "upload_file", {"filename": "notes.md"}, token=True))
        empty = client.post(_path(link["url"]), content=b"")
        unknown = client.post("/api/uploads/not-a-ticket", content=EBS.encode())
        used = client.post(_path(link["url"]), content=EBS.encode())
    assert empty.status_code == 400 and empty.json()["error"] == "bad_request"
    assert unknown.status_code == 404 and used.status_code == 404  # the empty try used the link


# --- REST -------------------------------------------------------------------------------------


def test_rest_upload_link_needs_a_token_and_matches_the_tool(open_app: OpenApp) -> None:
    with open_app(models=False) as client:
        refused = client.post("/api/upload-links", json={"filename": "a.pdf"})
        made = client.post("/api/upload-links", json={"filename": "a.md", "tags": ["x"]}, headers=AUTH)
        stored = client.post(_path(made.json()["url"]), content=EBS.encode())
    assert refused.status_code == 401
    assert made.status_code == 201 and made.json()["status"] == "upload_link"
    assert stored.status_code == 201


# --- stdio ------------------------------------------------------------------------------------


def test_stdio_upload_file_reads_the_local_path(tmp_path: Path) -> None:
    db_path = tmp_path / "s" / "kb.db"
    note = tmp_path / "runbook.md"
    note.write_text(EBS, encoding="utf-8")

    async def work(client: Any) -> Any:
        return await client.call_tool("upload_file", {"filename": str(note), "tags": ["ops"]})

    result = run_client(child_env(db_path, KB_TOKEN="testtoken"), tmp_path / "err.log", work)
    assert not result.is_error, result
    assert result.structured_content["status"] == "created"
    conn = db.connect(db_path)
    try:
        assert conn.execute("SELECT title FROM docs").fetchone()[0] == "Amazon EBS volumes"
    finally:
        conn.close()
