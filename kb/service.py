"""Shared operations used by MCP tools, REST routes and the web UI.

Every interface calls these functions; no business logic lives elsewhere. All
eleven operations are implemented: `search`, `get`, `upload`, `update`,
`delete`, `merge`, `ingest_url`, `list_tags`, `verify`, `stale` and `gaps`,
plus the admin helpers `set_fallback` and `audit` (read-only audit log), and
the one-time upload links `create_upload_link` and `upload_with_ticket`.

Write operations take the already-authenticated `actor` (the write token's
name) for the audit log; the token check itself happens in `kb.auth`, before
these functions are called. Failures raise `ServiceError` (or `IngestError`
from the conversion and fetch steps).

Writes that change chunks (`upload`, `ingest_url`, `update`, `merge`) take an
optional keyword-only `search_state` (the shared `SearchState`). With it, the
chunks are embedded before the write transaction and stored together with
their vectors, the vector index is updated after the commit, and unresolved
gaps are re-checked against the document. Without it only the database
changes and the next startup backfill embeds the chunks.

Write order, for every operation that stores chunks: convert and chunk (pure),
embed (outside any transaction), one `BEGIN IMMEDIATE` transaction, then the
after-commit steps (original to S3, index update, gap resolution), which log
failures and never raise because the change is already committed.
"""

from __future__ import annotations

import base64
import binascii
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

import numpy as np
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import ValidationError

from kb import backup, db, portable
from kb.config import Settings
from kb.errors import ServiceError
from kb import uploads
from kb.ingest import dedupe, mdmerge, originals
from kb.ingest.chunk import Chunk, chunk_markdown
from kb.ingest.convert import detect_format, to_markdown
from kb.ingest.errors import IngestError
from kb.ingest.fetch import fetch_url
from kb.schemas import (
    AuditEntry,
    AuditResult,
    ChunkOut,
    DeleteResult,
    DocOut,
    ExportResult,
    FallbackResult,
    GapOut,
    GapsResult,
    MergeResult,
    SearchResult,
    StaleDoc,
    StaleResult,
    TagCount,
    TagsResult,
    UpdateResult,
    UploadLink,
    UploadRequest,
    UploadResult,
    VerifyResult,
)
from kb.search import dupes, indexing, pipeline, resolve
from kb.search.state import SearchState
from kb.store import Store

log = logging.getLogger(__name__)

Conn = Store

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
_ORIGINAL_TYPES = {"html": "text/html", "pdf": "application/pdf"}


@dataclass(frozen=True)
class Original:
    """The uploaded file to keep in S3 (PDF and HTML only)."""

    filename: str
    data: bytes
    content_type: str


@dataclass(frozen=True)
class PreparedDoc:
    """Everything needed to store a document, computed without touching the DB.

    `chunks` are embedded from this object before the write transaction, which
    also feeds the near-duplicate check.
    """

    title: str
    markdown: str
    content_hash: str
    source_type: str
    source_url: str | None
    tags: list[str]
    chunks: list[Chunk]
    original: Original | None


# --- hooks ------------------------------------------------------------------------

NEAR_DUPLICATE_SKIPPED = "skipped: models unavailable"


def _after_chunks_written(conn: Conn, doc_id: int, search_state: SearchState | None = None) -> None:
    """Hook run after a doc's chunks are committed and its original saved.

    Publishes the doc's stored vectors to the in-memory index, replacing what
    the index held for the doc (so rewritten chunks leave no stale ids). The
    chunks were embedded before the transaction and stored with their vectors;
    chunks that could not be embedded then (models down) have no vector and are
    picked up by the next startup backfill. The document is already committed,
    so a failure is logged and never raised.
    """
    if search_state is None:
        return
    try:
        indexing.publish_doc(conn, search_state, doc_id)
    except Exception:
        log.error(
            "publishing vectors to the index failed; the next restart rebuilds it",
            exc_info=True,
            extra={"doc_id": doc_id},
        )


def _after_doc_deleted(doc_id: int, search_state: SearchState | None = None) -> None:
    """Hook run after a doc's rows are deleted: drop its vectors from the index.

    Logs instead of raising, like `_after_chunks_written`.
    """
    if search_state is None:
        return
    try:
        search_state.index.remove_doc(doc_id)
    except Exception:
        log.error("removing vectors after delete failed", exc_info=True, extra={"doc_id": doc_id})


# --- helpers --------------------------------------------------------------------


def _normalize_tags(tags: list[str]) -> list[str]:
    """Strip tags, drop empties and duplicates, keep first-seen order."""
    seen: dict[str, None] = {}
    for tag in tags:
        cleaned = tag.strip()
        if cleaned:
            seen.setdefault(cleaned, None)
    return list(seen)


def _check_source_url(url: str | None) -> str | None:
    """Return `url` stripped if it is http(s); reject other schemes (UI links it)."""
    if url is None or not url.strip():
        return None
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ServiceError("bad_source_url", "source_url must be an http or https URL", 400)
    return url


def _input_bytes(content: str, fmt: str, file_bytes: bytes | None) -> bytes | str:
    """The raw input for the converter: uploaded bytes, decoded PDF, or text.

    `content` is the JSON body field (base64 for `pdf`); `file_bytes`, when
    given, replaces it. Enforces the 20 MB limit.
    """
    if file_bytes is not None:
        data: bytes | str = file_bytes
    elif fmt == "pdf":
        try:
            data = base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ServiceError("bad_base64", "PDF content must be base64", 400) from exc
    else:
        data = content
    size = len(data) if isinstance(data, bytes) else len(data.encode("utf-8"))
    if size > MAX_UPLOAD_BYTES:
        raise ServiceError(
            "too_large", f"upload exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit", 413
        )
    return data


def _original_for(fmt: str, data: bytes | str, filename: str | None) -> Original | None:
    """The original to keep: HTML and PDF only; Markdown is the source of truth already."""
    content_type = _ORIGINAL_TYPES.get(fmt)
    if content_type is None:
        return None
    raw = data if isinstance(data, bytes) else data.encode("utf-8")
    return Original(originals.safe_filename(filename, f"original.{fmt}"), raw, content_type)


# --- upload ---------------------------------------------------------------------


def prepare_upload(
    req: UploadRequest, *, filename: str | None = None, file_bytes: bytes | None = None
) -> PreparedDoc:
    """Convert, clean, chunk and hash an upload. Pure: no database access.

    `file_bytes` (a multipart file) replaces `req.content` when given; then
    `filename` names the original saved to S3. Raises `IngestError` for
    conversion refusals and `ServiceError` for bad input.
    """
    fmt = detect_format(declared=req.format)
    source_url = _check_source_url(req.source_url)
    data = _input_bytes(req.content, fmt, file_bytes)
    converted = to_markdown(data, fmt, url=source_url)
    title = (req.title or "").strip() or converted.title
    chunks = chunk_markdown(converted.markdown, title)
    if not chunks:
        raise IngestError("empty_content", "the document has no text to store")
    return PreparedDoc(
        title=title,
        markdown=converted.markdown,
        content_hash=dedupe.content_hash(converted.markdown),
        source_type=converted.source_type,
        source_url=source_url,
        tags=_normalize_tags([*req.tags, *converted.tags]),
        chunks=chunks,
        original=_original_for(fmt, data, filename),
    )


def _insert_chunks(
    conn: Conn,
    doc_id: int,
    chunks: list[Chunk],
    vectors: np.ndarray | None,
    embed_model: str | None,
) -> None:
    """Insert `chunks` for `doc_id`, with their vectors when `vectors` is given."""
    conn.executemany(
        "INSERT INTO chunks (doc_id, ord, heading_path, text, embedding, embed_model)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                doc_id,
                c.ord,
                c.heading_path,
                c.text,
                None if vectors is None else np.ascontiguousarray(vectors[i], dtype=np.float32).tobytes(),
                None if vectors is None else embed_model,
            )
            for i, c in enumerate(chunks)
        ],
    )


def _audit(conn: Conn, actor: str, action: str, doc_id: int | None, note: str | None = None) -> None:
    """Append an audit row (call inside the write transaction)."""
    conn.execute(
        "INSERT INTO audit_log (token_name, action, doc_id, at, note) VALUES (?, ?, ?, ?, ?)",
        (actor, action, doc_id, db.utcnow(), note),
    )


def _embed_model(search_state: SearchState | None) -> str | None:
    """Name of the embedder whose vectors are stored (None without an embedder)."""
    embedder = None if search_state is None else search_state.embedder
    return None if embedder is None else embedder.model_name


def commit_prepared(
    conn: Conn,
    prepared: PreparedDoc,
    actor: str,
    vectors: np.ndarray | None = None,
    embed_model: str | None = None,
) -> tuple[int, bool]:
    """Write a prepared doc in one transaction.

    Inserts the doc, its chunks, its tags and an `upload` audit row. `vectors`
    (one row per chunk, made with `embed_model`) are stored with the chunks;
    without them the embedding stays NULL. The exact-duplicate check is repeated
    inside the transaction, so two concurrent identical uploads cannot both be
    stored. Returns `(doc_id, created)`; when `created` is False nothing was
    written and `doc_id` is the existing doc.
    """
    now = db.utcnow()
    with db.transaction(conn):
        existing = dedupe.find_exact_duplicate(conn, prepared.content_hash)
        if existing is not None:
            return existing, False
        doc_id = conn.insert(
            "INSERT INTO docs (title, source_url, source_type, content_md, content_hash,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                prepared.title,
                prepared.source_url,
                prepared.source_type,
                prepared.markdown,
                prepared.content_hash,
                now,
                now,
            ),
        )
        _insert_chunks(conn, doc_id, prepared.chunks, vectors, embed_model)
        conn.executemany(
            "INSERT OR IGNORE INTO doc_tags (doc_id, tag) VALUES (?, ?)",
            [(doc_id, tag) for tag in prepared.tags],
        )
        _audit(conn, actor, "upload", doc_id)
    return doc_id, True


def _store_original(conn: Conn, doc_id: int, original: Original | None) -> None:
    """Save the original to S3 and record `docs.s3_key`; failures are logged, not raised.

    The document is already committed and the original only serves later
    reprocessing, so an S3 outage must not turn a stored upload into an error.
    """
    if original is None:
        return
    try:
        key = originals.save_original(
            doc_id, original.filename, original.data, original.content_type
        )
    except (BotoCoreError, ClientError) as exc:
        code = exc.response.get("Error", {}).get("Code") if isinstance(exc, ClientError) else None
        log.warning(
            "saving original failed",
            extra={"doc_id": doc_id, "error_type": type(exc).__name__, "error_code": code},
        )
        return
    if key is not None:
        conn.execute("UPDATE docs SET s3_key = ? WHERE id = ?", (key, doc_id))


def _resolve_gaps_quietly(conn: Conn, search_state: SearchState | None, doc_id: int) -> None:
    """Run automatic gap resolution for `doc_id`; any failure is logged, never raised.

    Called last, after the result of the write is decided, so it cannot change
    what the caller is told. Gap bookkeeping is best effort.
    """
    try:
        resolve.resolve_gaps(conn, search_state, doc_id)
    except Exception:
        log.error("gap resolution failed", exc_info=True, extra={"doc_id": doc_id})


def _store_new(
    conn: Conn,
    prepared: PreparedDoc,
    force: bool,
    actor: str,
    search_state: SearchState | None,
) -> UploadResult:
    """Dedupe, embed, store and index a prepared doc (the body of `upload` and `ingest_url`).

    Order: exact-duplicate check; embed the chunks (outside the transaction);
    near-duplicate check unless `force`; `commit_prepared` storing the same
    vectors; original to S3; index update; gap resolution.
    """
    existing = dedupe.find_exact_duplicate(conn, prepared.content_hash)
    if existing is not None:
        return _duplicate(existing)
    vectors = indexing.embed_chunks(search_state, prepared.title, prepared.chunks)
    check_note: str | None = None
    if vectors is None:
        check_note = NEAR_DUPLICATE_SKIPPED
    elif not force:
        assert search_state is not None
        candidates = dupes.find_near_duplicates(conn, search_state, vectors)
        if candidates:
            best = candidates[0]
            return UploadResult(
                status="possible_duplicate",
                candidates=candidates,
                message=(
                    f"this looks like doc {best.doc_id} ({best.title!r}, similarity"
                    f" {best.similarity:.2f}); update or merge it, or send again with force=true"
                ),
            )
    doc_id, created = commit_prepared(conn, prepared, actor, vectors, _embed_model(search_state))
    if not created:
        return _duplicate(doc_id)
    _store_original(conn, doc_id, prepared.original)
    _after_chunks_written(conn, doc_id, search_state)
    log.info(
        "document uploaded",
        extra={"doc_id": doc_id, "actor": actor, "chunks": len(prepared.chunks)},
    )
    result = UploadResult(
        status="created",
        doc_id=doc_id,
        chunks=len(prepared.chunks),
        near_duplicate_check=check_note,
    )
    _resolve_gaps_quietly(conn, search_state, doc_id)
    return result


def upload(
    conn: Conn,
    req: UploadRequest,
    actor: str,
    *,
    filename: str | None = None,
    file_bytes: bytes | None = None,
    search_state: SearchState | None = None,
) -> UploadResult:
    """Convert, dedupe, chunk, embed and store a new document.

    Steps: `prepare_upload`; exact-duplicate check (`duplicate` with the
    existing `doc_id`, nothing written); embed the chunks; near-duplicate check
    (`possible_duplicate` with up to 3 `candidates`, nothing written, unless
    `req.force`); `commit_prepared` (the vectors are stored with the chunks);
    save the original to S3 and set `docs.s3_key`; update the vector index;
    resolve gaps the new doc answers. When the models are unavailable the near
    check is skipped and the result says so in `near_duplicate_check`.
    """
    prepared = prepare_upload(req, filename=filename, file_bytes=file_bytes)
    return _store_new(conn, prepared, req.force, actor, search_state)


def _duplicate(doc_id: int) -> UploadResult:
    return UploadResult(
        status="duplicate",
        doc_id=doc_id,
        message=f"identical content is already stored as doc {doc_id}",
    )


def ingest_url(
    conn: Conn,
    url: str,
    actor: str,
    *,
    tags: list[str] | None = None,
    force: bool = False,
    search_state: SearchState | None = None,
    transport: Any = None,
) -> UploadResult:
    """Fetch one page (SSRF-guarded), then follow the `upload` path.

    The format comes from the response (URL extension, then Content-Type) and
    `source_url` is the final URL after redirects. HTML and PDF originals go to
    S3 like any upload. Returns the same `UploadResult` as `upload`. Raises
    `IngestError` (`bad_scheme`, `blocked_address`, `fetch_failed`, `too_large`,
    and the conversion errors). `transport` is for tests (an
    `httpx.MockTransport`) and is not part of the REST API.
    """
    fetched = fetch_url(url, transport=transport)
    fmt = detect_format(content_type=fetched.content_type, url=fetched.final_url)
    try:
        req = UploadRequest(
            content="", format=fmt, tags=tags or [], source_url=fetched.final_url, force=force
        )
    except ValidationError as exc:  # e.g. a redirect target longer than 2000 characters
        raise ServiceError("bad_source_url", "the final URL is too long to store", 400) from exc
    prepared = prepare_upload(req, file_bytes=fetched.body)
    return _store_new(conn, prepared, force, actor, search_state)


# --- upload links (whole files from a chat client) -------------------------------

UPLOAD_EXTENSIONS = (".pdf", ".md", ".markdown", ".txt", ".html", ".htm")


def create_upload_link(
    actor: str,
    filename: str,
    base_url: str,
    *,
    title: str | None = None,
    tags: list[str] | None = None,
    force: bool = False,
    store: uploads.TicketStore | None = None,
) -> UploadLink:
    """Issue a one-time link through which `actor` can send one file (see `kb/uploads.py`).

    `filename` only names the file (its extension picks the format); a path is
    reduced to its last part. `base_url` is the public address of this server
    (`KB_PUBLIC_URL`, or the request's own base URL). Raises `ServiceError`
    `bad_format` for an extension the converter cannot read.
    """
    name = originals.safe_filename(filename, "")
    if not name or not name.lower().endswith(UPLOAD_EXTENSIONS):
        raise ServiceError(
            "bad_format", "filename must end in .pdf, .md, .markdown, .txt, .html or .htm", 400
        )
    clean_title = (title or "").strip() or None
    if clean_title is not None and len(clean_title) > 500:
        raise ServiceError("bad_request", "title must be at most 500 characters", 400)
    ticket, ttl = (store or uploads.STORE).issue(
        actor, name, title=clean_title, tags=_normalize_tags(tags or []), force=force
    )
    url = f"{base_url.rstrip('/')}/api/uploads/{ticket}"
    minutes = int(ttl // 60)
    log.info("upload link issued", extra={"actor": actor, "upload_filename": name})
    return UploadLink(
        url=url,
        filename=name,
        expires_in=int(ttl),
        curl=f'curl -sS --data-binary @"<path to {name}>" "{url}"',
        message=(
            f"Send the file's bytes as the body of a POST to `url` within {minutes} minutes, for"
            " example with the `curl` command (replace the placeholder with the local path). The"
            " link works once. The response is the usual upload result; for `possible_duplicate`"
            " ask for a new link with force=true if the user wants it saved anyway."
        ),
    )


def upload_with_ticket(
    conn: Conn,
    ticket: str,
    data: bytes,
    *,
    search_state: SearchState | None = None,
    store: uploads.TicketStore | None = None,
) -> UploadResult:
    """Store `data` as the file a ticket was issued for, as the ticket's owner.

    The ticket is used up even if the file then fails to convert. Raises
    `ServiceError` `bad_ticket` (404) for an unknown, used or expired ticket and
    `bad_request` for an empty body; conversion errors are `IngestError`.
    """
    issued = (store or uploads.STORE).take(ticket)
    if issued is None:
        raise ServiceError(
            "bad_ticket", "this upload link is unknown, already used or expired; ask for a new one", 404
        )
    if not data:
        raise ServiceError("bad_request", "the request body is empty; send the file's bytes", 400)
    req = UploadRequest(
        title=issued.title,
        content="",
        format=detect_format(filename=issued.filename),
        tags=list(issued.tags),
        force=issued.force,
    )
    return upload(
        conn, req, issued.actor, filename=issued.filename, file_bytes=data, search_state=search_state
    )


# --- get and delete ---------------------------------------------------------------


def get(
    conn: Conn,
    doc_id: int | None = None,
    chunk_id: int | None = None,
    include_chunks: bool = False,
) -> DocOut:
    """Fetch a document by `doc_id`, or the document owning `chunk_id`.

    Give exactly one of `doc_id` and `chunk_id`. With `chunk_id` the parent doc
    is returned with all its chunks and the requested one has `matched=True`.
    Raises `ServiceError` (`bad_request` 400, `not_found` 404).
    """
    if (doc_id is None) == (chunk_id is None):
        raise ServiceError("bad_request", "give exactly one of doc_id and chunk_id", 400)
    if chunk_id is not None:
        row = conn.execute("SELECT doc_id FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
        if row is None:
            raise ServiceError("not_found", f"chunk {chunk_id} not found", 404)
        doc_id = int(row["doc_id"])
        include_chunks = True
    doc = conn.execute("SELECT * FROM docs WHERE id = ?", (doc_id,)).fetchone()
    if doc is None:
        raise ServiceError("not_found", f"doc {doc_id} not found", 404)
    tags = [
        r["tag"]
        for r in conn.execute("SELECT tag FROM doc_tags WHERE doc_id = ? ORDER BY tag", (doc_id,))
    ]
    chunk_rows = conn.execute(
        "SELECT id, ord, heading_path, text FROM chunks WHERE doc_id = ? ORDER BY ord", (doc_id,)
    ).fetchall()
    chunks = (
        [
            ChunkOut(
                id=r["id"],
                ord=r["ord"],
                heading_path=r["heading_path"],
                text=r["text"],
                matched=r["id"] == chunk_id,
            )
            for r in chunk_rows
        ]
        if include_chunks
        else None
    )
    return DocOut(
        id=doc["id"],
        title=doc["title"],
        source_url=doc["source_url"],
        source_type=doc["source_type"],
        content_md=doc["content_md"],
        tags=tags,
        chunk_count=len(chunk_rows),
        created_at=doc["created_at"],
        updated_at=doc["updated_at"],
        verified_at=doc["verified_at"],
        verified_by=doc["verified_by"],
        chunks=chunks,
    )


def delete(
    conn: Conn, doc_id: int, actor: str, *, search_state: SearchState | None = None
) -> DeleteResult:
    """Delete a document; chunks, tags and FTS rows go with it (cascade, triggers).

    Writes a `delete` audit row in the same transaction, then runs
    `_after_doc_deleted`. The original in S3 is kept. Raises `ServiceError`
    `not_found` (404) when the doc does not exist.
    """
    with db.transaction(conn):
        cur = conn.execute("DELETE FROM docs WHERE id = ?", (doc_id,))
        if cur.rowcount == 0:
            raise ServiceError("not_found", f"doc {doc_id} not found", 404)
        conn.execute(
            "INSERT INTO audit_log (token_name, action, doc_id, at) VALUES (?, 'delete', ?, ?)",
            (actor, doc_id, db.utcnow()),
        )
    _after_doc_deleted(doc_id, search_state)
    log.info("document deleted", extra={"doc_id": doc_id, "actor": actor})
    return DeleteResult(doc_id=doc_id)


async def search(
    state: SearchState, query: str, k: int = 5, tags: list[str] | None = None
) -> SearchResult:
    """Search the knowledge base (semantic with keyword fallback).

    Args:
        state: the shared `SearchState` (models, vector index, toggle).
        query: natural-language question; must not be blank.
        k: maximum number of results, clamped to 1..20.
        tags: if given, only documents that carry all of these tags.

    Returns:
        `SearchResult` with `mode`, `confidence` and `results`. Low-confidence
        queries are recorded in `gaps`. Raises `ServiceError` `invalid_request`
        (422) for a blank query. Async because embedding and reranking run in
        worker threads under a timeout.
    """
    cleaned = query.strip()
    if not cleaned:
        raise ServiceError("invalid_request", "query must not be empty", 422)
    return await pipeline.search(state, cleaned, k, tags)


def set_fallback(conn: Conn, state: SearchState, on: bool, actor: str) -> FallbackResult:
    """Turn the keyword-fallback toggle on or off and write an audit row.

    While on, every search uses keyword mode even if the models are healthy
    (used to demo the fallback). The flag lives in process memory and resets
    to off on restart.
    """
    state.fallback_toggle = on
    conn.execute(
        "INSERT INTO audit_log (token_name, action, doc_id, at) VALUES (?, ?, NULL, ?)",
        (actor, "fallback_on" if on else "fallback_off", db.utcnow()),
    )
    log.info("fallback toggle changed", extra={"fallback_toggle": on, "actor": actor})
    return FallbackResult(fallback_toggle=on)


def export_database(conn: Conn, settings: Settings, actor: str) -> ExportResult:
    """Write the PostgreSQL contents to the S3 snapshot (`snapshots/kb-latest.db.gz`) now.

    The result is a SQLite file with ids and embeddings, restorable like any
    snapshot (`kb.portable`). In SQLite mode the app already snapshots after every
    write, so this refuses with `bad_request`. Needs `KB_BUCKET`. Writes an
    `export` audit row once the upload succeeded. `bad_gateway` (502) when S3
    or the copy fails.
    """
    if conn.dialect != "postgres":
        raise ServiceError(
            "bad_request", "the database is SQLite; it is snapshotted after every write", 400
        )
    if not settings.bucket:
        raise ServiceError("bad_request", "KB_BUCKET is not set", 400)
    target = f"s3://{settings.bucket}/{backup.SNAPSHOT_KEY}"
    try:
        counts = portable.export_to(conn, target, region=settings.aws_region).counts
    except backup.BackupError as exc:
        log.error("export failed", extra={"actor": actor, "error": str(exc)})
        raise ServiceError("bad_gateway", "the export to S3 failed; see the server log", 502)
    with db.transaction(conn):
        _audit(conn, actor, "export", None, f"{counts['docs']} docs, {counts['chunks']} chunks")
    log.info("database exported", extra={"actor": actor, "counts": counts})
    return ExportResult(key=backup.SNAPSHOT_KEY, docs=counts["docs"], chunks=counts["chunks"])


# --- update ----------------------------------------------------------------------


def _require_doc(conn: Conn, doc_id: int) -> Any:
    row = conn.execute("SELECT * FROM docs WHERE id = ?", (doc_id,)).fetchone()
    if row is None:
        raise ServiceError("not_found", f"doc {doc_id} not found", 404)
    return row


def _doc_tags(conn: Conn, doc_id: int) -> list[str]:
    return [
        r["tag"]
        for r in conn.execute("SELECT tag FROM doc_tags WHERE doc_id = ? ORDER BY tag", (doc_id,))
    ]


def _check_hash_free(conn: Conn, content_hash: str, own_ids: set[int]) -> None:
    """Refuse (409 `duplicate`) new content that equals another doc's content."""
    other = dedupe.find_exact_duplicate(conn, content_hash)
    if other is not None and other not in own_ids:
        raise ServiceError(
            "duplicate", f"identical content is already stored as doc {other}", 409
        )


def _check_unchanged(conn: Conn, seen: dict[int, tuple[str, str]]) -> None:
    """Inside a write transaction: the docs still have the content and title that were read.

    The chunks are embedded before the transaction starts, so another writer may
    have changed a doc in between; then the prepared chunks would be wrong and
    the caller must retry (409 `conflict`). A doc deleted in between is a 404.
    """
    for doc_id, (content_hash, title) in seen.items():
        row = conn.execute(
            "SELECT content_hash, title FROM docs WHERE id = ?", (doc_id,)
        ).fetchone()
        if row is None:
            raise ServiceError("not_found", f"doc {doc_id} not found", 404)
        if row["content_hash"] != content_hash or row["title"] != title:
            raise ServiceError(
                "conflict", f"doc {doc_id} changed while the operation was running; try again", 409
            )


def _replace_chunks(
    conn: Conn,
    doc_id: int,
    chunks: list[Chunk],
    vectors: np.ndarray | None,
    embed_model: str | None,
) -> None:
    """Swap a doc's chunks (FTS rows follow through the triggers)."""
    conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
    _insert_chunks(conn, doc_id, chunks, vectors, embed_model)


def _chunk_count(conn: Conn, doc_id: int) -> int:
    return int(conn.execute("SELECT count(*) FROM chunks WHERE doc_id = ?", (doc_id,)).fetchone()[0])


def update(
    conn: Conn,
    doc_id: int,
    actor: str,
    *,
    content: str | None = None,
    title: str | None = None,
    tags: list[str] | None = None,
    format: str = "markdown",
    file_bytes: bytes | None = None,
    search_state: SearchState | None = None,
) -> UpdateResult:
    """Change a document's content, title or tags.

    A tags-only change replaces the doc's tag set and touches nothing else (no
    re-embedding, `updated_at` unchanged). A content or title change re-chunks
    and re-embeds (the embedded text contains the title, and heading paths can
    start with it), updates `content_hash` and `updated_at`, replaces the
    chunks, refreshes the index after commit, resolves gaps the new text
    answers, and writes an `update` audit row. `content` is converted like an
    upload of the same `format` (`markdown` by default, `html`, or `pdf` as
    base64) and cleaned; the converted title, tags and front matter are
    ignored, and no original is kept. `file_bytes` (a multipart file or a
    fetched page) replaces `content`. `tags=[]` clears the tags.
    Returns `UpdateResult` with `status="unchanged"` when nothing differs.
    Raises `ServiceError`: `bad_request` (nothing given, blank title),
    `not_found`, `duplicate` (409, the new content equals another doc's),
    `conflict` (409, the doc changed while we embedded), `too_large`.
    """
    has_content = content is not None or file_bytes is not None
    if not has_content and title is None and tags is None:
        raise ServiceError("bad_request", "give at least one of content, title and tags", 400)
    doc = _require_doc(conn, doc_id)
    new_title = doc["title"]
    if title is not None:
        new_title = title.strip()
        if not new_title:
            raise ServiceError("bad_request", "title must not be blank", 400)
    new_md = doc["content_md"]
    if has_content:
        fmt = detect_format(declared=format)
        data = _input_bytes(content or "", fmt, file_bytes)
        new_md = to_markdown(data, fmt, url=doc["source_url"]).markdown
    new_hash = dedupe.content_hash(new_md)
    content_changed = new_hash != doc["content_hash"]
    rechunk = content_changed or new_title != doc["title"]
    old_tags = _doc_tags(conn, doc_id)
    new_tags = old_tags if tags is None else _normalize_tags(tags)
    tags_changed = set(new_tags) != set(old_tags)
    if not rechunk and not tags_changed:
        return UpdateResult(status="unchanged", doc_id=doc_id, chunks=_chunk_count(conn, doc_id))

    chunks: list[Chunk] = []
    vectors: np.ndarray | None = None
    if rechunk:
        if content_changed:
            _check_hash_free(conn, new_hash, {doc_id})
        chunks = chunk_markdown(new_md, new_title)
        if not chunks:
            raise IngestError("empty_content", "the document has no text to store")
        vectors = indexing.embed_chunks(search_state, new_title, chunks)
    with db.transaction(conn):
        if rechunk:
            _check_unchanged(conn, {doc_id: (doc["content_hash"], doc["title"])})
            _check_hash_free(conn, new_hash, {doc_id})
            conn.execute(
                "UPDATE docs SET title = ?, content_md = ?, content_hash = ?, updated_at = ?"
                " WHERE id = ?",
                (new_title, new_md, new_hash, db.utcnow(), doc_id),
            )
            _replace_chunks(conn, doc_id, chunks, vectors, _embed_model(search_state))
        if tags_changed:
            conn.execute("DELETE FROM doc_tags WHERE doc_id = ?", (doc_id,))
            conn.executemany(
                "INSERT OR IGNORE INTO doc_tags (doc_id, tag) VALUES (?, ?)",
                [(doc_id, tag) for tag in new_tags],
            )
        _audit(conn, actor, "update", doc_id)
    if rechunk:
        _after_chunks_written(conn, doc_id, search_state)
    log.info(
        "document updated",
        extra={"doc_id": doc_id, "actor": actor, "rechunked": rechunk, "tags_changed": tags_changed},
    )
    result = UpdateResult(
        status="updated", doc_id=doc_id, rechunked=rechunk, chunks=_chunk_count(conn, doc_id)
    )
    if rechunk:
        _resolve_gaps_quietly(conn, search_state, doc_id)
    return result


def update_from_url(
    conn: Conn,
    doc_id: int,
    url: str,
    actor: str,
    *,
    search_state: SearchState | None = None,
    transport: Any = None,
) -> UpdateResult:
    """Fetch one page (SSRF-guarded) and use it as the new content of `doc_id`.

    The format comes from the response (URL extension, then Content-Type);
    everything else is `update`. Used by the add page's "Update existing" for a
    URL that looked like a near-duplicate. `transport` is for tests.
    """
    fetched = fetch_url(url, transport=transport)
    fmt = detect_format(content_type=fetched.content_type, url=fetched.final_url)
    return update(
        conn, doc_id, actor, format=fmt, file_bytes=fetched.body, search_state=search_state
    )


# --- merge -----------------------------------------------------------------------


def merge(
    conn: Conn,
    target_id: int,
    source_ids: list[int],
    actor: str,
    *,
    delete_sources: bool = True,
    search_state: SearchState | None = None,
) -> MergeResult:
    """Append source docs to the target as sections and re-chunk the target.

    Each source becomes `## <title>`, a `Source: <url>` line when it has a URL,
    and its body with every heading demoted one level (see
    `kb.ingest.mdmerge`). The target is re-chunked and re-embedded, gets the
    union of the sources' tags, and keeps its own title and URL. With
    `delete_sources` the sources are deleted and gaps they had resolved now
    point at the target. All database changes happen in one transaction; the
    index is updated after commit. One audit row per participating doc
    (`merge` for the target, `merge_source` for each source). Raises
    `ServiceError`: `bad_request` (no sources, target among the sources),
    `not_found` (any unknown id), `duplicate`, `conflict`.
    """
    sources = list(dict.fromkeys(source_ids))
    if not sources:
        raise ServiceError("bad_request", "give at least one source doc", 400)
    if target_id in sources:
        raise ServiceError("bad_request", "a document cannot be merged into itself", 400)
    wanted = [target_id, *sources]
    marks = ",".join("?" * len(wanted))
    rows = {
        r["id"]: r for r in conn.execute(f"SELECT * FROM docs WHERE id IN ({marks})", wanted)
    }
    missing = [i for i in wanted if i not in rows]
    if missing:
        raise ServiceError(
            "not_found", "doc " + ", ".join(str(i) for i in missing) + " not found", 404
        )
    target = rows[target_id]
    merged_md = mdmerge.merge_markdown(
        target["content_md"],
        [mdmerge.MergeSource(rows[i]["title"], rows[i]["source_url"], rows[i]["content_md"]) for i in sources],
    )
    new_hash = dedupe.content_hash(merged_md)
    _check_hash_free(conn, new_hash, {target_id})
    chunks = chunk_markdown(merged_md, target["title"])
    vectors = indexing.embed_chunks(search_state, target["title"], chunks)
    source_marks = ",".join("?" * len(sources))
    source_tags = [
        r["tag"]
        for r in conn.execute(
            f"SELECT DISTINCT tag FROM doc_tags WHERE doc_id IN ({source_marks}) ORDER BY tag",
            sources,
        )
    ]
    seen = {i: (rows[i]["content_hash"], rows[i]["title"]) for i in wanted}
    with db.transaction(conn):
        _check_unchanged(conn, seen)
        if delete_sources:
            conn.execute(
                f"UPDATE gaps SET resolved_doc_id = ? WHERE resolved_doc_id IN ({source_marks})",
                [target_id, *sources],
            )
            conn.execute(f"DELETE FROM docs WHERE id IN ({source_marks})", sources)
        _check_hash_free(conn, new_hash, {target_id})
        conn.execute(
            "UPDATE docs SET content_md = ?, content_hash = ?, updated_at = ? WHERE id = ?",
            (merged_md, new_hash, db.utcnow(), target_id),
        )
        _replace_chunks(conn, target_id, chunks, vectors, _embed_model(search_state))
        conn.executemany(
            "INSERT OR IGNORE INTO doc_tags (doc_id, tag) VALUES (?, ?)",
            [(target_id, tag) for tag in source_tags],
        )
        _audit(conn, actor, "merge", target_id)
        for source_id in sources:
            _audit(conn, actor, "merge_source", source_id)
    if delete_sources:
        for source_id in sources:
            _after_doc_deleted(source_id, search_state)
    _after_chunks_written(conn, target_id, search_state)
    log.info(
        "documents merged",
        extra={"doc_id": target_id, "actor": actor, "sources": sources, "deleted": delete_sources},
    )
    result = MergeResult(
        doc_id=target_id,
        merged=sources,
        deleted=sources if delete_sources else [],
        chunks=len(chunks),
    )
    _resolve_gaps_quietly(conn, search_state, target_id)
    return result


# --- verify, stale, gaps, tags, audit ------------------------------------------------

_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
MAX_LIST_LIMIT = 200


def _clamp_limit(limit: int) -> int:
    return max(1, min(int(limit), MAX_LIST_LIMIT))


def verify(conn: Conn, doc_id: int, actor: str, *, note: str | None = None) -> VerifyResult:
    """Mark a document as verified now (UTC) by `actor`.

    Sets `verified_at` and `verified_by` and writes a `verify` audit row that
    carries the optional `note` (at most 500 characters). `updated_at` is left
    alone. Raises `ServiceError` `not_found` or `bad_request` (note too long).
    """
    note = (note or "").strip() or None
    if note is not None and len(note) > 500:
        raise ServiceError("bad_request", "note must be at most 500 characters", 400)
    now = db.utcnow()
    with db.transaction(conn):
        cur = conn.execute(
            "UPDATE docs SET verified_at = ?, verified_by = ? WHERE id = ?", (now, actor, doc_id)
        )
        if cur.rowcount == 0:
            raise ServiceError("not_found", f"doc {doc_id} not found", 404)
        _audit(conn, actor, "verify", doc_id, note)
    log.info("document verified", extra={"doc_id": doc_id, "actor": actor})
    return VerifyResult(doc_id=doc_id, verified_at=now, verified_by=actor)


def stale(conn: Conn, days: int = 90, limit: int = 20) -> StaleResult:
    """Docs not verified or updated for at least `days` days, oldest first.

    A doc's last touch is the later of `verified_at` and `updated_at` (which
    starts out equal to `created_at`). `days_since` counts whole days since then.
    `stale(days=0)` lists every doc. `limit` is clamped to 1..200; a negative
    `days` raises `ServiceError` `bad_request`.
    """
    if days < 0:
        raise ServiceError("bad_request", "days must not be negative", 400)
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).strftime(_TIME_FORMAT)
    last = (
        "CASE WHEN verified_at IS NOT NULL AND verified_at > COALESCE(updated_at, created_at)"
        " THEN verified_at ELSE COALESCE(updated_at, created_at) END"
    )
    rows = conn.execute(
        f"SELECT id, title, source_url, updated_at, verified_at, {last} AS last FROM docs"
        f" WHERE {last} <= ? ORDER BY last ASC, id ASC LIMIT ?",
        (cutoff, _clamp_limit(limit)),
    ).fetchall()
    docs = [
        StaleDoc(
            doc_id=r["id"],
            title=r["title"],
            source_url=r["source_url"],
            updated_at=r["updated_at"],
            verified_at=r["verified_at"],
            days_since=max(
                0,
                (now - datetime.strptime(r["last"], _TIME_FORMAT).replace(tzinfo=timezone.utc)).days,
            ),
        )
        for r in rows
    ]
    return StaleResult(docs=docs)


def gaps(conn: Conn, limit: int = 20, include_resolved: bool = False) -> GapsResult:
    """Unanswered questions grouped by normalized text, most frequent first.

    Each group has an example `query` (the latest asked), its `count`, the best
    `top_score`, `last_seen` and `resolved_doc_id`. Unless `include_resolved`,
    only rows that are still unresolved are counted, so a question that was
    resolved and then asked again shows up with the newer count. Order: count,
    then `last_seen`, both descending. `limit` is clamped to 1..200.
    """
    where = "" if include_resolved else "WHERE resolved_doc_id IS NULL"
    rows = conn.execute(
        "SELECT m.query_norm, g.query, m.n, m.top, m.last_seen, m.resolved FROM ("
        " SELECT query_norm, count(*) AS n, max(top_score) AS top, max(created_at) AS last_seen,"
        f" max(resolved_doc_id) AS resolved, max(id) AS last_id FROM gaps {where}"
        " GROUP BY query_norm) m JOIN gaps g ON g.id = m.last_id"
        " ORDER BY m.n DESC, m.last_seen DESC, m.last_id DESC LIMIT ?",
        (_clamp_limit(limit),),
    ).fetchall()
    return GapsResult(
        gaps=[
            GapOut(
                query_norm=r["query_norm"],
                query=r["query"],
                count=r["n"],
                top_score=r["top"],
                last_seen=r["last_seen"],
                resolved_doc_id=r["resolved"],
            )
            for r in rows
        ]
    )


def list_tags(conn: Conn) -> TagsResult:
    """Every tag with its document count, most used first (ties by name)."""
    rows = conn.execute(
        "SELECT tag, count(*) AS n FROM doc_tags GROUP BY tag ORDER BY n DESC, tag"
    ).fetchall()
    return TagsResult(tags=[TagCount(tag=r["tag"], count=r["n"]) for r in rows])


def audit(conn: Conn, limit: int = 20) -> AuditResult:
    """The most recent audit rows, newest first (`limit` clamped to 1..200).

    Shows token names, never token secrets. `note` is free text from the
    caller (a verify note), so interfaces must escape it when rendering.
    """
    rows = conn.execute(
        "SELECT id, token_name, action, doc_id, at, note FROM audit_log ORDER BY id DESC LIMIT ?",
        (_clamp_limit(limit),),
    ).fetchall()
    return AuditResult(
        entries=[
            AuditEntry(
                id=r["id"],
                token_name=r["token_name"],
                action=r["action"],
                doc_id=r["doc_id"],
                at=r["at"],
                note=r["note"],
            )
            for r in rows
        ]
    )
