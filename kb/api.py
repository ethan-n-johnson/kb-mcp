"""REST routes: thin wrappers over `kb.service`, mounted at `/api`.

No business logic lives here. Errors become `ErrorOut` JSON: `ServiceError`
and `IngestError` keep their codes, validation problems give 422, and any
other exception gives a 500 `{"error": "internal"}` with no stack trace.
"""

from __future__ import annotations

import logging
from typing import Iterator

from fastapi import APIRouter, Depends, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from kb import service
from kb.store import Store, open_store
from kb.auth import require_writer
from kb.errors import ServiceError, ingest_status
from kb.ingest.convert import detect_format
from kb.ingest.errors import IngestError
from kb.ratelimit import check_search_rate
from kb.schemas import (
    UploadLink,
    AuditResult,
    DeleteResult,
    DocOut,
    ErrorOut,
    FallbackRequest,
    ExportResult,
    FallbackResult,
    GapsResult,
    IngestRequest,
    MergeRequest,
    MergeResult,
    SearchResult,
    StaleResult,
    TagsResult,
    UpdateRequest,
    UpdateResult,
    UploadRequest,
    UploadResult,
    VerifyRequest,
    VerifyResult,
)
from kb.search.state import SearchState

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

_ERRORS = {"model": ErrorOut}


def get_conn(request: Request) -> Iterator[Store]:
    """Per-request database connection, closed when the request ends.

    A connection per request keeps `BEGIN IMMEDIATE` transactions from
    different threads apart (the thread pool runs sync routes concurrently).
    """
    conn = open_store(request.app.state.settings)
    try:
        yield conn
    finally:
        conn.close()


def get_search(request: Request) -> SearchState:
    """The shared search state (models, vector index, fallback toggle)."""
    return request.app.state.search


Conn = Depends(get_conn)
Writer = Depends(require_writer)
Search = Depends(get_search)


def _upload_response(result: UploadResult) -> JSONResponse:
    """201 for a stored doc; 200 for `possible_duplicate`; 409 for an exact duplicate.

    A near-duplicate is a normal outcome the caller acts on (update, merge or
    send again with `force=true`), so it is a 200 with `status` and
    `candidates`, not an error.
    """
    if result.status == "created":
        return JSONResponse(result.payload(), status_code=201)
    if result.status == "possible_duplicate":
        return JSONResponse(result.payload(), status_code=200)
    body = {"error": result.status, "detail": result.message or ""}
    body.update(result.model_dump(exclude_none=True, exclude_defaults=True, exclude={"message"}))
    return JSONResponse(body, status_code=409)


@router.post("/docs", status_code=201, responses={409: _ERRORS, 401: _ERRORS, 422: _ERRORS})
def upload_doc(
    req: UploadRequest, conn=Conn, actor: str = Writer, search: SearchState = Search
) -> JSONResponse:
    """Upload a document as JSON (`content` is base64 for `format="pdf"`)."""
    return _upload_response(service.upload(conn, req, actor, search_state=search))


@router.post("/docs/file", status_code=201, responses={409: _ERRORS, 401: _ERRORS, 422: _ERRORS})
def upload_file(
    file: UploadFile = File(...),
    title: str | None = Form(None),
    tags: str | None = Form(None),
    force: bool = Form(False),
    conn=Conn,
    actor: str = Writer,
    search: SearchState = Search,
) -> JSONResponse:
    """Upload a file as multipart; the format comes from the extension or type."""
    data = file.file.read(service.MAX_UPLOAD_BYTES + 1)
    if len(data) > service.MAX_UPLOAD_BYTES:
        raise ServiceError("too_large", "upload exceeds the 20 MB limit", 413)
    fmt = detect_format(filename=file.filename, content_type=file.content_type)
    req = UploadRequest(
        title=title or None,
        content="",
        format=fmt,
        tags=[t for t in (tags or "").split(",") if t.strip()],
        force=force,
    )
    result = service.upload(
        conn, req, actor, filename=file.filename, file_bytes=data, search_state=search
    )
    return _upload_response(result)


@router.post("/docs/merge", response_model=MergeResult, responses={404: _ERRORS, 401: _ERRORS, 409: _ERRORS})
def merge_docs(
    body: MergeRequest, conn=Conn, actor: str = Writer, search: SearchState = Search
) -> MergeResult:
    """Append source docs to the target as sections, re-chunk it, optionally delete the sources."""
    return service.merge(
        conn, body.target_id, body.source_ids, actor,
        delete_sources=body.delete_sources, search_state=search,
    )


@router.post("/ingest", status_code=201, responses={409: _ERRORS, 401: _ERRORS, 422: _ERRORS, 502: _ERRORS})
def ingest(
    body: IngestRequest, conn=Conn, actor: str = Writer, search: SearchState = Search
) -> JSONResponse:
    """Fetch one page and store it like an upload (201, 200 `possible_duplicate`, 409 duplicate)."""
    result = service.ingest_url(
        conn, body.url, actor, tags=body.tags, force=body.force, search_state=search
    )
    return _upload_response(result)


class UploadLinkRequest(BaseModel):
    """Body of `POST /api/upload-links`."""

    filename: str = Field(max_length=255)
    title: str | None = Field(default=None, max_length=500)
    tags: list[str] = Field(default_factory=list)
    force: bool = False


def public_base_url(request: Request) -> str:
    """The address clients reach this server at: `KB_PUBLIC_URL`, else the request's own."""
    return request.app.state.settings.public_url or str(request.base_url).rstrip("/")


@router.post("/upload-links", status_code=201, response_model=UploadLink, responses={400: _ERRORS, 401: _ERRORS})
def create_upload_link(body: UploadLinkRequest, request: Request, actor: str = Writer) -> UploadLink:
    """A one-time link for sending one whole file (same as the MCP `upload_file` tool)."""
    return service.create_upload_link(
        actor, body.filename, public_base_url(request),
        title=body.title, tags=body.tags, force=body.force,
    )


@router.api_route(
    "/uploads/{ticket}", methods=["POST", "PUT"], status_code=201,
    responses={400: _ERRORS, 404: _ERRORS, 409: _ERRORS, 413: _ERRORS, 422: _ERRORS},
)
async def upload_with_ticket(ticket: str, request: Request, search: SearchState = Search) -> JSONResponse:
    """Receive the file for a one-time link: the raw request body is the file.

    No bearer token: the ticket in the path is the permission, valid once and
    for 15 minutes (`kb/uploads.py`). Same responses as `POST /api/docs/file`,
    plus 404 `bad_ticket`. `PUT` is accepted too, so `curl -T file URL` works.
    """
    data = await request.body()
    settings = request.app.state.settings

    def work() -> JSONResponse:
        conn = open_store(settings)
        try:
            return _upload_response(
                service.upload_with_ticket(conn, ticket, data, search_state=search)
            )
        finally:
            conn.close()

    return await run_in_threadpool(work)


@router.get("/docs/{doc_id}", response_model=DocOut, responses={404: _ERRORS})
def get_doc(doc_id: int, include_chunks: bool = False, conn=Conn) -> DocOut:
    """Return a document with its tags; `include_chunks=true` adds the chunks."""
    return service.get(conn, doc_id=doc_id, include_chunks=include_chunks)


@router.patch(
    "/docs/{doc_id}", response_model=UpdateResult, responses={404: _ERRORS, 401: _ERRORS, 409: _ERRORS}
)
def update_doc(
    doc_id: int, body: UpdateRequest, conn=Conn, actor: str = Writer, search: SearchState = Search
) -> UpdateResult:
    """Change content, title or tags; content or title changes re-chunk and re-embed.

    `format` (`markdown`, `html`, `pdf` as base64) says how `content` is converted.
    """
    return service.update(
        conn, doc_id, actor,
        content=body.content, title=body.title, tags=body.tags, format=body.format,
        search_state=search,
    )


@router.post("/docs/{doc_id}/verify", response_model=VerifyResult, responses={404: _ERRORS, 401: _ERRORS})
def verify_doc(
    doc_id: int, body: VerifyRequest | None = None, conn=Conn, actor: str = Writer
) -> VerifyResult:
    """Mark the doc as verified now by the calling token; the optional note goes to the audit log."""
    return service.verify(conn, doc_id, actor, note=body.note if body else None)


@router.get("/chunks/{chunk_id}", response_model=DocOut, responses={404: _ERRORS})
def get_chunk(chunk_id: int, conn=Conn) -> DocOut:
    """Return the document owning a chunk, with all chunks and the requested one marked."""
    return service.get(conn, chunk_id=chunk_id)


@router.delete("/docs/{doc_id}", response_model=DeleteResult, responses={404: _ERRORS, 401: _ERRORS})
def delete_doc(
    doc_id: int, conn=Conn, actor: str = Writer, search: SearchState = Search
) -> DeleteResult:
    """Delete a document with its chunks, tags and index vectors."""
    return service.delete(conn, doc_id, actor, search_state=search)


@router.get("/search", response_model=SearchResult, responses={422: _ERRORS})
async def search_docs(
    request: Request,
    q: str = Query(..., min_length=1),
    k: int = Query(5, ge=1),
    tags: str | None = None,
    search: SearchState = Search,
) -> SearchResult:
    """Search the knowledge base. `k` is capped at 20; `tags=a,b` requires all tags.

    Rate limited per client address (`KB_SEARCH_RATE`); over the limit gives 429.
    """
    check_search_rate(request)
    wanted = [t for t in (tags or "").split(",") if t.strip()]
    return await service.search(search, q, k, wanted)


@router.get("/tags", response_model=TagsResult)
def list_tags(conn=Conn) -> TagsResult:
    """Tags with their document counts, most used first."""
    return service.list_tags(conn)


@router.get("/stale", response_model=StaleResult, responses={400: _ERRORS})
def stale_docs(days: int = Query(90, ge=0), limit: int = Query(20, ge=1, le=200), conn=Conn) -> StaleResult:
    """Docs neither verified nor updated for `days` days, oldest first."""
    return service.stale(conn, days, limit)


@router.get("/gaps", response_model=GapsResult)
def list_gaps(
    limit: int = Query(20, ge=1, le=200), include_resolved: bool = False, conn=Conn
) -> GapsResult:
    """Unanswered questions grouped by normalized text, most frequent first."""
    return service.gaps(conn, limit, include_resolved)


@router.get("/audit", response_model=AuditResult)
def audit_log(limit: int = Query(20, ge=1, le=200), conn=Conn) -> AuditResult:
    """Recent audit rows, newest first. Token names only, never secrets."""
    return service.audit(conn, limit)


@router.post("/admin/fallback", response_model=FallbackResult, responses={401: _ERRORS})
def set_fallback(
    body: FallbackRequest, conn=Conn, actor: str = Writer, search: SearchState = Search
) -> FallbackResult:
    """Force keyword search on or off (for the demo). Requires a write token."""
    return service.set_fallback(conn, search, body.on, actor)


@router.post("/admin/export", response_model=ExportResult, responses={401: _ERRORS, 400: _ERRORS})
def export_database(request: Request, conn=Conn, actor: str = Writer) -> ExportResult:
    """Write the PostgreSQL contents to the S3 snapshot now (`infra/up.sh save` calls this)."""
    return service.export_database(conn, request.app.state.settings, actor)


# --- error handling -----------------------------------------------------------


def _error(status: int, code: str, detail: str = "", headers: dict[str, str] | None = None) -> JSONResponse:
    body = ErrorOut(error=code, detail=detail).model_dump()
    return JSONResponse(body, status_code=status, headers=headers)


def register_error_handlers(app: FastAPI) -> None:
    """Install JSON error handlers for `/api` on `app`."""

    @app.exception_handler(ServiceError)
    async def _service_error(request: Request, exc: ServiceError) -> JSONResponse:
        headers = dict(exc.headers)
        if exc.http_status == 401:
            headers["WWW-Authenticate"] = "Bearer"
        return _error(exc.http_status, exc.code, exc.message, headers or None)

    @app.exception_handler(IngestError)
    async def _ingest_error(request: Request, exc: IngestError) -> JSONResponse:
        return _error(ingest_status(exc.code), exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'] if p != 'body')}: {e['msg']}"
            for e in exc.errors()
        )
        return _error(422, "invalid_request", problems)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if not request.url.path.startswith("/api"):
            return await http_exception_handler(request, exc)
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return _error(exc.status_code, code, str(exc.detail), dict(exc.headers or {}) or None)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.error(
            "unhandled error",
            exc_info=(type(exc), exc, exc.__traceback__),
            extra={"path": request.url.path, "method": request.method},
        )
        return _error(500, "internal")
