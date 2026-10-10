"""Pydantic models shared by REST routes and the MCP tools."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class UploadRequest(BaseModel):
    """Body of `POST /api/docs` and the argument bundle of `service.upload`.

    `format` is `markdown`, `html` or `pdf` (a few aliases such as `md` work).
    For `pdf` over JSON, `content` is base64. `title` may be omitted; it is then
    taken from front matter, the first H1 or the page title. `force` stores the
    document even when it looks like a near-duplicate of an existing one.
    """

    title: str | None = Field(default=None, max_length=500)
    content: str
    format: str = "markdown"
    tags: list[str] = Field(default_factory=list)
    source_url: str | None = Field(default=None, max_length=2000)
    force: bool = False


class ChunkOut(BaseModel):
    """One chunk of a document; `matched` marks the chunk asked for by id."""

    id: int
    ord: int
    heading_path: str
    text: str
    matched: bool = False


class DocOut(BaseModel):
    """A full document with metadata and tags. `chunks` is set on request."""

    id: int
    title: str
    source_url: str | None
    source_type: str
    content_md: str
    tags: list[str]
    chunk_count: int
    created_at: str
    updated_at: str
    verified_at: str | None
    verified_by: str | None
    chunks: list[ChunkOut] | None = None


class DuplicateCandidate(BaseModel):
    """An existing doc that looks like the upload; `similarity` is the cosine of
    the two documents' mean chunk vectors (0 to 1, higher is closer)."""

    doc_id: int
    title: str
    similarity: float
    source_url: str | None = None


class UploadResult(BaseModel):
    """Outcome of an upload.

    `created`: new doc stored, `chunks` is its chunk count.
    `duplicate`: exact content already stored, `doc_id` is the existing doc.
    `possible_duplicate`: near match, nothing was written, `candidates` lists up
    to 3 existing docs; the caller chooses `update`, `merge` or re-sends with
    `force=true`. `near_duplicate_check` is set only when the check could not run
    (`"skipped: models unavailable"`).
    """

    status: Literal["created", "duplicate", "possible_duplicate"]
    doc_id: int | None = None
    chunks: int | None = None
    candidates: list[DuplicateCandidate] = Field(default_factory=list)
    message: str | None = None
    near_duplicate_check: str | None = None

    def payload(self) -> dict[str, Any]:
        """The wire form shared by REST and MCP: no nulls, and for `created` and
        `duplicate` no default-valued fields (an empty `candidates` list)."""
        if self.status == "possible_duplicate":
            return self.model_dump(exclude_none=True)
        return self.model_dump(exclude_none=True, exclude_defaults=True)


class UploadLink(BaseModel):
    """A one-time link for sending a whole file (`upload_file` tool, `kb/uploads.py`).

    Send the file's bytes as the request body to `url` with `method` before
    `expires_in` seconds pass; the response is the same `UploadResult` an upload
    returns. `curl` is a ready command with a placeholder for the local path.
    """

    status: Literal["upload_link"] = "upload_link"
    url: str
    method: Literal["POST"] = "POST"
    filename: str
    expires_in: int
    curl: str
    message: str


class DeleteResult(BaseModel):
    """Outcome of a delete."""

    status: Literal["deleted"] = "deleted"
    doc_id: int


class ErrorOut(BaseModel):
    """Body of every REST error: a stable code and a safe message."""

    error: str
    detail: str = ""


class SearchHit(BaseModel):
    """One search result. `snippet` is plain text in semantic mode and contains
    `<b>...</b>` highlight markers (and nothing else trusted) in keyword mode."""

    doc_id: int
    chunk_id: int
    title: str
    heading_path: str
    snippet: str
    score: float
    source_url: str | None = None
    verified_at: str | None = None


class SearchResult(BaseModel):
    """Response of `search`, shared by REST and MCP (see PLAN.md, Search)."""

    mode: Literal["semantic", "keyword"]
    confidence: Literal["high", "medium", "low"]
    results: list[SearchHit] = Field(default_factory=list)


class FallbackRequest(BaseModel):
    """Body of `POST /api/admin/fallback`."""

    on: bool


class FallbackResult(BaseModel):
    """Current state of the keyword-fallback toggle."""

    fallback_toggle: bool


class IngestRequest(BaseModel):
    """Body of `POST /api/ingest`: fetch one page and store it like an upload."""

    url: str = Field(max_length=2000)
    tags: list[str] = Field(default_factory=list)
    force: bool = False


class UpdateRequest(BaseModel):
    """Body of `PATCH /api/docs/{id}`. Give at least one field.

    `content` is converted like an upload of the same `format` (`markdown` by
    default, `html`, or `pdf` as base64). `tags`, when given, replaces the
    whole tag set (`[]` clears it).
    """

    content: str | None = None
    title: str | None = Field(default=None, max_length=500)
    tags: list[str] | None = None
    format: str = "markdown"


class UpdateResult(BaseModel):
    """Outcome of an update. `rechunked` is true when chunks and vectors were rebuilt.

    `unchanged` means nothing differed from what is stored, so nothing was written.
    """

    status: Literal["updated", "unchanged"]
    doc_id: int
    rechunked: bool = False
    chunks: int


class MergeRequest(BaseModel):
    """Body of `POST /api/docs/merge`."""

    target_id: int
    source_ids: list[int] = Field(min_length=1, max_length=50)
    delete_sources: bool = True


class MergeResult(BaseModel):
    """Outcome of a merge: the target's new chunk count and which sources were folded in."""

    status: Literal["merged"] = "merged"
    doc_id: int
    merged: list[int]
    deleted: list[int]
    chunks: int


class VerifyRequest(BaseModel):
    """Optional body of `POST /api/docs/{id}/verify`."""

    note: str | None = Field(default=None, max_length=500)


class VerifyResult(BaseModel):
    """Outcome of a verify."""

    status: Literal["verified"] = "verified"
    doc_id: int
    verified_at: str
    verified_by: str


class TagCount(BaseModel):
    """A tag and the number of documents that carry it."""

    tag: str
    count: int


class TagsResult(BaseModel):
    """Response of `list_tags`, most used first."""

    tags: list[TagCount]


class StaleDoc(BaseModel):
    """A document nobody has verified or updated for a while."""

    doc_id: int
    title: str
    source_url: str | None = None
    updated_at: str
    verified_at: str | None = None
    days_since: int


class StaleResult(BaseModel):
    """Response of `stale`, oldest first."""

    docs: list[StaleDoc]


class GapOut(BaseModel):
    """Repeated unanswered questions that share a normalized form.

    `top_score` is the best score seen; scores from keyword mode are BM25
    values, not comparable to semantic ones. `resolved_doc_id` is only set when
    `include_resolved` was requested.
    """

    query_norm: str
    query: str
    count: int
    top_score: float | None = None
    last_seen: str
    resolved_doc_id: int | None = None


class GapsResult(BaseModel):
    """Response of `gaps`, most frequent first."""

    gaps: list[GapOut]


class AuditEntry(BaseModel):
    """One audit row. `token_name` is the write token's name, never its secret."""

    id: int
    token_name: str | None
    action: str
    doc_id: int | None
    at: str
    note: str | None = None


class AuditResult(BaseModel):
    """Response of `audit`, newest first."""

    entries: list[AuditEntry]
