"""The 12 MCP tools: thin wrappers over `kb.service`, shared by HTTP and stdio.

`build_mcp(state)` returns an `MCPServer` (the `mcp` 2.x name of what 1.x called
`FastMCP`) with the tool set from PLAN.md. Tools hold no business logic: they
check the write token (write tools only), call one `kb.service` function in a
worker thread and return the pydantic result as a plain dict, the same shape the
REST route returns.

Errors a caller can act on (`ServiceError`, `IngestError`, bad arguments) become
`ToolError`, which the SDK reports as a result with `is_error=True` and the
message. Anything else is a crash: the SDK logs it and the client sees only
"Error executing tool <name>".

Token rules: over HTTP the token is the request's `Authorization: Bearer`
header; over stdio there is no request, so it is `KB_TOKEN`. Both go through
`auth.check_token`.
"""

import logging
import os
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, TypeVar

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field, ValidationError
from starlette.concurrency import run_in_threadpool

from kb import db, service
from kb.auth import bearer_secret, check_token
from kb.errors import ServiceError
from kb.ingest.convert import detect_format
from kb.ingest.errors import IngestError
from kb.ratelimit import check_search_rate
from kb.schemas import UploadRequest
from kb.search.state import SearchState

log = logging.getLogger(__name__)

T = TypeVar("T")

WRITE_TOKEN_MESSAGE = "This tool needs a valid write token"
"""Shown for every failed token check; it never says which part failed."""

TOOL_NAMES = (
    "search",
    "get",
    "upload",
    "upload_file",
    "update",
    "delete",
    "merge",
    "ingest_url",
    "list_tags",
    "verify",
    "stale",
    "gaps",
)

INSTRUCTIONS = (
    "Team knowledge base for the Cloud Computing course (AWS). Search it before answering "
    "questions about our systems, AWS setup or runbooks. Reading needs no token; tools that "
    "change the knowledge base need a write token."
)

DESCRIPTIONS: dict[str, str] = {
    "search": (
        "Search the team knowledge base before answering questions about our systems, AWS "
        "setup or runbooks. Check `confidence`: if it is low, tell the user the knowledge "
        "base has no good answer instead of guessing. Each result has the document title, "
        "heading path, a snippet and the source URL; use `get` with its `chunk_id` or "
        "`doc_id` to read the full text. `tags` limits results to documents that carry all "
        "of the given tags."
    ),
    "get": (
        "Read one document in full (Markdown plus metadata and tags) after `search` found it "
        "or when the user names a document id. Give `doc_id`, or give the `chunk_id` of a "
        "search result to get its parent document with that chunk marked `matched`. Set "
        "`include_chunks` to see how the document is split."
    ),
    "upload": (
        "Save a new note or document to the knowledge base. Use it when the user asks you "
        "to remember, record or add something. `format` is `markdown` (default), `html` or "
        "`pdf` (for pdf, `content` is base64). If the result is `possible_duplicate`, show "
        "the candidates and ask whether to update one of them, merge, or save anyway with "
        "force=true. A `duplicate` result means identical content is already stored. "
        "Needs a write token."
    ),
    "upload_file": (
        "Save a whole file (PDF, Markdown, text or HTML) from disk. Use it when the user "
        "attaches or points to a file to keep; prefer it over `upload` for any file, since "
        "nothing is copied into the tool call. Over HTTP it returns a one-time `url` (valid 15 minutes, "
        "one file): send the file's bytes as the body of a POST there, for example with the "
        "returned `curl` command after replacing the placeholder with the file's path, and "
        "read the upload result from the response. On a local (stdio) server `filename` is "
        "the file's path and the file is stored right away. Needs a write token."
    ),
    "update": (
        "Change an existing document: new `content` (Markdown by default; set `format` to "
        "`html`, or `pdf` with base64 content, to have it converted like an upload), a new "
        "`title`, or a new full `tags` list (tags replace the old set; an empty list clears "
        "them). Use it to fix or refresh a document instead of uploading a second copy. "
        "Needs a write token."
    ),
    "delete": (
        "Permanently remove a document with its chunks, tags and search entries. Only use "
        "it when the user clearly asks for the deletion; prefer `update` or `merge` for "
        "cleanup. Needs a write token."
    ),
    "merge": (
        "Fold duplicate or overlapping documents into one: the `source_ids` documents are "
        "appended to `target_id` as sections (their source URLs and tags are kept) and, "
        "unless `delete_sources` is false, removed. Use it after an upload reports "
        "`possible_duplicate`, or when the user asks to combine notes. Needs a write token."
    ),
    "ingest_url": (
        "Fetch one public web page or PDF by URL and store it in the knowledge base. Use it "
        "when the user gives a link to documentation they want kept; it cannot read pages "
        "behind a login. Like `upload`, it can answer `possible_duplicate` or `duplicate`. "
        "Needs a write token."
    ),
    "list_tags": (
        "List every tag with the number of documents that carry it, most used first. Use it "
        "to learn which topics the knowledge base covers or to pick a valid tag for the "
        "`tags` filter of `search`."
    ),
    "verify": (
        "Record that a person checked a document and it is still correct (sets "
        "`verified_at` and `verified_by`). Use it after the user confirms a document is "
        "current, typically one listed by `stale`. `note` is saved in the audit log. "
        "Needs a write token."
    ),
    "stale": (
        "List documents nobody has verified or updated for `days` days, oldest first. Use "
        "it when asked what documentation may be out of date or what needs review."
    ),
    "gaps": (
        "List questions the knowledge base could not answer, most frequent first. Use when "
        "asked what documentation is missing or what people searched for without success. "
        "Questions answered by a later upload are hidden unless `include_resolved` is true."
    ),
}

READ_ONLY = ToolAnnotations(readOnlyHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

SearchStateSource = SearchState | Callable[[], SearchState]


def require_writer_mcp(ctx: Context, *, stdio: bool = False) -> str:
    """Return the name of the write token behind this tool call, or raise `ToolError`.

    Over HTTP the secret comes from the request's `Authorization: Bearer` header
    (`ctx.request_context.request`). With `stdio=True` there is no request and
    the secret is the `KB_TOKEN` environment variable; an HTTP call that somehow
    carries no request is refused rather than falling back to the environment.
    Every failure raises the same message, `WRITE_TOKEN_MESSAGE`. A token store
    that cannot be read also denies (and is logged), so the check fails closed.
    """
    request = ctx.request_context.request
    secret: str | None
    if request is not None:
        secret = bearer_secret(request.headers.get("authorization"))
    elif stdio:
        secret = os.environ.get("KB_TOKEN") or None
    else:
        secret = None
    try:
        name = check_token(secret)
    except Exception:
        log.error("write token check failed", exc_info=True)
        name = None
    if name is None:
        raise ToolError(WRITE_TOKEN_MESSAGE)
    return name


def _as_tool_error(exc: Exception) -> Exception:
    """The `ToolError` that stands for an anticipated failure; any other error unchanged.

    Messages carry the stable code and the already safe message of the service
    error, or only the names of the invalid fields for argument problems.
    """
    if isinstance(exc, (ServiceError, IngestError)):
        err = ToolError(f"{exc.code}: {exc.message}")
    elif isinstance(exc, ValidationError):
        fields = ", ".join(sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()}))
        err = ToolError(f"invalid_request: check {fields}")
    else:
        return exc
    err.__cause__ = exc
    return err


def build_mcp(source: SearchStateSource, *, stdio: bool = False) -> MCPServer:
    """Build the MCP server with the 12 tools.

    `source` is the shared `SearchState`, or a function returning it: the HTTP
    app creates its state in the FastAPI lifespan, after the tools are built.
    `stdio=True` makes write tools read the token from `KB_TOKEN`.
    """

    def state() -> SearchState:
        return source() if callable(source) else source

    async def call_read(fn: Callable[[sqlite3.Connection], T]) -> T:
        """Run `fn(conn)` in a worker thread on its own short-lived connection."""

        def work() -> T:
            conn = db.connect(state().db_path)
            try:
                return fn(conn)
            finally:
                conn.close()

        try:
            return await run_in_threadpool(work)
        except Exception as exc:
            raise _as_tool_error(exc)

    async def call_write(ctx: Context, fn: Callable[[sqlite3.Connection, str], T]) -> T:
        """Check the token, then run `fn(conn, actor)` in a worker thread.

        The token check comes first, so a refused call never opens the database.
        """

        def work() -> T:
            actor = require_writer_mcp(ctx, stdio=stdio)
            conn = db.connect(state().db_path)
            try:
                return fn(conn, actor)
            finally:
                conn.close()

        try:
            return await run_in_threadpool(work)
        except Exception as exc:
            raise _as_tool_error(exc)

    mcp = MCPServer("kb", instructions=INSTRUCTIONS)

    @mcp.tool(name="search", description=DESCRIPTIONS["search"], annotations=READ_ONLY)
    async def search(
        ctx: Context,
        query: Annotated[str, Field(description="The question or keywords to look for")],
        k: Annotated[int, Field(description="Maximum number of results (1 to 20)")] = 5,
        tags: Annotated[list[str] | None, Field(description="Only documents with all of these tags")] = None,
    ) -> dict[str, Any]:
        try:
            # Over HTTP the request carries the client address; stdio has none
            # and is never limited.
            check_search_rate(ctx.request_context.request)
            result = await service.search(state(), query, k, tags or [])
        except ServiceError as exc:
            if exc.code == "rate_limited":
                raise ToolError(exc.message)
            raise _as_tool_error(exc)
        return result.model_dump()

    @mcp.tool(name="get", description=DESCRIPTIONS["get"], annotations=READ_ONLY)
    async def get(
        doc_id: Annotated[int | None, Field(description="Id of the document")] = None,
        chunk_id: Annotated[int | None, Field(description="Id of a chunk; returns its parent document")] = None,
        include_chunks: Annotated[bool, Field(description="Also return the document's chunks")] = False,
    ) -> dict[str, Any]:
        doc = await call_read(
            lambda conn: service.get(
                conn, doc_id=doc_id, chunk_id=chunk_id, include_chunks=include_chunks
            )
        )
        return doc.model_dump()

    @mcp.tool(name="upload", description=DESCRIPTIONS["upload"], annotations=WRITE)
    async def upload(
        ctx: Context,
        title: Annotated[str, Field(description="Document title")],
        content: Annotated[str, Field(description="The document text (base64 for pdf)")],
        format: Annotated[str, Field(description="markdown, html or pdf")] = "markdown",
        tags: Annotated[list[str] | None, Field(description="Tags to attach")] = None,
        force: Annotated[bool, Field(description="Save even if it looks like a near-duplicate")] = False,
    ) -> dict[str, Any]:
        def run(conn: sqlite3.Connection, actor: str) -> dict[str, Any]:
            req = UploadRequest(
                title=title, content=content, format=format, tags=tags or [], force=force
            )
            return service.upload(conn, req, actor, search_state=state()).payload()

        return await call_write(ctx, run)

    @mcp.tool(name="upload_file", description=DESCRIPTIONS["upload_file"], annotations=WRITE)
    async def upload_file(
        ctx: Context,
        filename: Annotated[
            str,
            Field(
                description="The file's name, e.g. guide.pdf (its extension picks the format); "
                "on a local stdio server, its path",
                max_length=1000,
            ),
        ],
        title: Annotated[str | None, Field(description="Document title (default: from the file)")] = None,
        tags: Annotated[list[str] | None, Field(description="Tags to attach")] = None,
        force: Annotated[bool, Field(description="Save even if it looks like a near-duplicate")] = False,
    ) -> dict[str, Any]:
        request = ctx.request_context.request
        if request is None and stdio:
            path = Path(filename).expanduser()

            def run_local(conn: sqlite3.Connection, actor: str) -> dict[str, Any]:
                try:
                    data = path.read_bytes()
                except OSError as exc:
                    raise ServiceError("bad_path", f"cannot read {path}: {exc.strerror}", 400) from exc
                req = UploadRequest(
                    title=title, content="", format=detect_format(filename=path.name),
                    tags=tags or [], force=force,
                )
                return service.upload(
                    conn, req, actor, filename=path.name, file_bytes=data, search_state=state()
                ).payload()

            return await call_write(ctx, run_local)

        def run_link(conn: sqlite3.Connection, actor: str) -> dict[str, Any]:
            base = state().settings.public_url
            if base is None:
                assert request is not None
                base = str(request.base_url).rstrip("/")
            return service.create_upload_link(
                actor, filename, base, title=title, tags=tags, force=force
            ).model_dump()

        return await call_write(ctx, run_link)

    @mcp.tool(name="update", description=DESCRIPTIONS["update"], annotations=WRITE)
    async def update(
        ctx: Context,
        doc_id: Annotated[int, Field(description="Id of the document to change")],
        content: Annotated[str | None, Field(description="New content (Markdown unless `format` says otherwise)")] = None,
        title: Annotated[str | None, Field(description="New title")] = None,
        tags: Annotated[list[str] | None, Field(description="New full tag list; replaces the old one")] = None,
        format: Annotated[str, Field(description="Format of `content`: markdown, html or pdf (base64)")] = "markdown",
    ) -> dict[str, Any]:
        return (
            await call_write(
                ctx,
                lambda conn, actor: service.update(
                    conn, doc_id, actor,
                    content=content, title=title, tags=tags, format=format,
                    search_state=state(),
                ),
            )
        ).model_dump()

    @mcp.tool(name="delete", description=DESCRIPTIONS["delete"], annotations=DESTRUCTIVE)
    async def delete(
        ctx: Context,
        doc_id: Annotated[int, Field(description="Id of the document to delete")],
    ) -> dict[str, Any]:
        return (
            await call_write(
                ctx,
                lambda conn, actor: service.delete(conn, doc_id, actor, search_state=state()),
            )
        ).model_dump()

    @mcp.tool(name="merge", description=DESCRIPTIONS["merge"], annotations=DESTRUCTIVE)
    async def merge(
        ctx: Context,
        target_id: Annotated[int, Field(description="Id of the document that receives the others")],
        source_ids: Annotated[list[int], Field(description="Ids of the documents to fold in", min_length=1, max_length=50)],
        delete_sources: Annotated[bool, Field(description="Delete the sources afterwards")] = True,
    ) -> dict[str, Any]:
        return (
            await call_write(
                ctx,
                lambda conn, actor: service.merge(
                    conn, target_id, source_ids, actor,
                    delete_sources=delete_sources, search_state=state(),
                ),
            )
        ).model_dump()

    @mcp.tool(name="ingest_url", description=DESCRIPTIONS["ingest_url"], annotations=WRITE)
    async def ingest_url(
        ctx: Context,
        url: Annotated[str, Field(description="http(s) address of the page or PDF", max_length=2000)],
        tags: Annotated[list[str] | None, Field(description="Tags to attach")] = None,
        force: Annotated[bool, Field(description="Save even if it looks like a near-duplicate")] = False,
    ) -> dict[str, Any]:
        return (
            await call_write(
                ctx,
                lambda conn, actor: service.ingest_url(
                    conn, url, actor, tags=tags or [], force=force, search_state=state()
                ),
            )
        ).payload()

    @mcp.tool(name="list_tags", description=DESCRIPTIONS["list_tags"], annotations=READ_ONLY)
    async def list_tags() -> dict[str, Any]:
        return (await call_read(service.list_tags)).model_dump()

    @mcp.tool(name="verify", description=DESCRIPTIONS["verify"], annotations=WRITE)
    async def verify(
        ctx: Context,
        doc_id: Annotated[int, Field(description="Id of the document that was checked")],
        note: Annotated[str | None, Field(description="What was checked (saved in the audit log)", max_length=500)] = None,
    ) -> dict[str, Any]:
        return (
            await call_write(
                ctx, lambda conn, actor: service.verify(conn, doc_id, actor, note=note)
            )
        ).model_dump()

    @mcp.tool(name="stale", description=DESCRIPTIONS["stale"], annotations=READ_ONLY)
    async def stale(
        days: Annotated[int, Field(description="Age in days after which a document is stale", ge=0)] = 90,
        limit: Annotated[int, Field(description="Maximum number of documents (1 to 200)", ge=1, le=200)] = 20,
    ) -> dict[str, Any]:
        return (await call_read(lambda conn: service.stale(conn, days, limit))).model_dump()

    @mcp.tool(name="gaps", description=DESCRIPTIONS["gaps"], annotations=READ_ONLY)
    async def gaps(
        limit: Annotated[int, Field(description="Maximum number of questions (1 to 200)", ge=1, le=200)] = 20,
        include_resolved: Annotated[bool, Field(description="Also list questions a later upload answered")] = False,
    ) -> dict[str, Any]:
        return (
            await call_read(lambda conn: service.gaps(conn, limit, include_resolved))
        ).model_dump()

    return mcp
