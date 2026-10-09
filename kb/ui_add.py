"""The add page: paste Markdown, upload a file, or fetch a URL.

Each form posts to its own route. When the result is `possible_duplicate`, the
buttons in the result fragment send the same form again (htmx includes the
original form's fields, file included) with an `action`:

- `save`: store it (the first submit, and the default);
- `force`: store it even though it looks like a near-duplicate ("Save anyway");
- `update`: replace the content of `candidate` with the new content;
- `merge`: store it with `force=true`, then fold it into `candidate`.

The routes only choose which service calls to make; conversion, duplicate
checks and writes all happen in `kb.service`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse

from kb import service
from kb.auth import require_writer
from kb.errors import ServiceError
from kb.ingest.convert import detect_format
from kb.schemas import UpdateResult, UploadRequest, UploadResult
from kb.search.state import SearchState
from kb.ui_common import Conn, Search, page, ui_errors

router = APIRouter()

ACTIONS = ("save", "force", "update", "merge")


def _tags(raw: str) -> list[str]:
    return [t for t in raw.split(",") if t.strip()]


@dataclass
class Source:
    """One add form's input: how to store it as a new doc, and how to update an old one with it."""

    kind: str  # paste, file or url (names the form and result element ids)
    store: Callable[[bool], UploadResult]
    update: Callable[[int], UpdateResult]


def _finish(
    request: Request,
    conn: sqlite3.Connection,
    search: SearchState,
    actor: str,
    action: str,
    candidate: int | None,
    source: Source,
) -> HTMLResponse:
    """Run `action` for `source` and render the result fragment."""
    if action not in ACTIONS:
        raise ServiceError("bad_request", f"unknown action {action!r}", 400)
    if action in ("update", "merge") and candidate is None:
        raise ServiceError("bad_request", "choose the document to update or merge into", 400)
    context = {"source": source.kind}
    if action == "update":
        assert candidate is not None
        updated = source.update(candidate)
        return page(request, "_add_result.html", kind="updated", result=updated, **context)
    stored = source.store(action in ("force", "merge"))
    if action == "merge" and stored.status == "created":
        assert candidate is not None and stored.doc_id is not None
        try:
            merged = service.merge(conn, candidate, [stored.doc_id], actor, search_state=search)
        except ServiceError as exc:
            raise ServiceError(
                exc.code, f"saved as doc {stored.doc_id}, but the merge failed: {exc.message}", exc.http_status
            ) from exc
        return page(request, "_add_result.html", kind="merged", result=merged, **context)
    return page(request, "_add_result.html", kind=stored.status, result=stored, **context)


@router.post("/ui/add/paste", response_class=HTMLResponse)
@ui_errors
def add_paste(
    request: Request,
    content: str = Form(""),
    title: str = Form(""),
    tags: str = Form(""),
    action: str = Form("save"),
    candidate: int | None = Form(None),
    conn: sqlite3.Connection = Conn,
    search: SearchState = Search,
) -> HTMLResponse:
    """Store pasted Markdown, or act on a duplicate warning for it."""
    actor = require_writer(request)
    if not content.strip():
        raise ServiceError("invalid_request", "paste some Markdown first", 422)

    def store(force: bool) -> UploadResult:
        req = UploadRequest(title=title.strip() or None, content=content, tags=_tags(tags), force=force)
        return service.upload(conn, req, actor, search_state=search)

    def update(doc_id: int) -> UpdateResult:
        return service.update(conn, doc_id, actor, content=content, search_state=search)

    return _finish(request, conn, search, actor, action, candidate, Source("paste", store, update))


@router.post("/ui/add/file", response_class=HTMLResponse)
@ui_errors
def add_file(
    request: Request,
    file: UploadFile | None = File(None),
    title: str = Form(""),
    tags: str = Form(""),
    action: str = Form("save"),
    candidate: int | None = Form(None),
    conn: sqlite3.Connection = Conn,
    search: SearchState = Search,
) -> HTMLResponse:
    """Store an uploaded Markdown, HTML or PDF file, or act on a duplicate warning for it."""
    actor = require_writer(request)
    if file is None or not getattr(file, "filename", ""):
        raise ServiceError("invalid_request", "choose a file first", 422)
    data = file.file.read(service.MAX_UPLOAD_BYTES + 1)
    if len(data) > service.MAX_UPLOAD_BYTES:
        raise ServiceError("too_large", "upload exceeds the 20 MB limit", 413)
    fmt = detect_format(filename=file.filename, content_type=file.content_type)

    def store(force: bool) -> UploadResult:
        req = UploadRequest(
            title=title.strip() or None, content="", format=fmt, tags=_tags(tags), force=force
        )
        return service.upload(conn, req, actor, filename=file.filename, file_bytes=data, search_state=search)

    def update(doc_id: int) -> UpdateResult:
        return service.update(
            conn, doc_id, actor, format=fmt, file_bytes=data, search_state=search
        )

    return _finish(request, conn, search, actor, action, candidate, Source("file", store, update))


@router.post("/ui/add/url", response_class=HTMLResponse)
@ui_errors
def add_url(
    request: Request,
    url: str = Form(""),
    tags: str = Form(""),
    action: str = Form("save"),
    candidate: int | None = Form(None),
    conn: sqlite3.Connection = Conn,
    search: SearchState = Search,
) -> HTMLResponse:
    """Fetch and store a page, or act on a duplicate warning for it."""
    actor = require_writer(request)
    if not url.strip():
        raise ServiceError("invalid_request", "enter a URL first", 422)

    def store(force: bool) -> UploadResult:
        return service.ingest_url(
            conn, url.strip(), actor, tags=_tags(tags), force=force, search_state=search
        )

    def update(doc_id: int) -> UpdateResult:
        return service.update_from_url(conn, doc_id, url.strip(), actor, search_state=search)

    return _finish(request, conn, search, actor, action, candidate, Source("url", store, update))
