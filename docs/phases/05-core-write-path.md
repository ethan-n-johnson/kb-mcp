# Phase 05: Core write path

**Day:** 2 (Thu Oct 8) · **Depends on:** 04 · **Where:** local · **PLAN.md sections:** Ingestion and data model (steps 5 and 6, duplicate rules, schema); Interfaces (upload, get, delete rows); Auth and security (Tokens bullet)

## Goal

Upload, get and delete through `service.py` and REST, with bearer-token auth, audit logging, exact-hash dedupe and originals saved to S3. Embeddings come in phase 07; this phase leaves clean hooks for them.

## Read first

- `CLAUDE.md`, `docs/PROGRESS.md` (deviation 4)
- `docs/PLAN.md`: "Ingestion and data model", the `upload`, `get`, `delete` rows in "Interfaces", "Auth and security"

## Tasks

1. `kb/auth.py`:
   - `check_token(raw: str | None) -> str | None` returns the token **name** for a valid secret, comparing with `hmac.compare_digest` against every configured token (no early exit on the first mismatch).
   - FastAPI dependency `require_writer(request) -> str` reading `Authorization: Bearer <secret>`; 401 with `WWW-Authenticate: Bearer` on missing or bad tokens. Never log the secret.
2. `kb/schemas.py`: Pydantic models shared by REST and (later) MCP: `UploadRequest(title, content, format, tags=[], source_url=None, force=False)`, `DocOut`, `ChunkOut`, `UploadResult(status: "created" | "duplicate" | "possible_duplicate", doc_id, ...)`, `ErrorOut(error, detail)`. For `format="pdf"` over JSON, `content` is base64.
3. `kb/ingest/originals.py`: `save_original(doc_id, filename, data, content_type) -> str | None`. Uses boto3 `put_object` to `originals/<doc_id>/<filename>` in `KB_BUCKET`; returns `None` without calling AWS when `KB_BUCKET` is unset. Only PDF and HTML inputs are saved.
4. `kb/service.py`, implement:
   - `upload(conn, req, actor) -> UploadResult`: convert (phase 04), clean, chunk; normalize the Markdown (strip, collapse whitespace) and SHA-256 it; if the hash exists return `status="duplicate"` with the existing `doc_id` and write nothing. Otherwise, in one transaction, insert `docs`, `chunks` (embedding NULL for now), `doc_tags` and an `audit_log` row (`action="upload"`). After commit, save the original to S3 and set `docs.s3_key`; then call the hook `_after_chunks_written(conn, doc_id)`.
   - `get(conn, doc_id=None, chunk_id=None, include_chunks=False) -> DocOut`: full Markdown, metadata, tags; with `chunk_id`, return the parent doc and mark that chunk.
   - `delete(conn, doc_id, actor)`: delete the doc (cascade handles chunks, tags, FTS), audit row, then hook `_after_doc_deleted(doc_id)`.
   - The two hooks are no-ops now; phase 07 makes them update embeddings and the index. Name them exactly as above.
   - Errors raise `ServiceError(code, message, http_status)`.
5. `kb/api.py` router mounted at `/api`:
   - `POST /api/docs` (JSON, auth), `POST /api/docs/file` (multipart: `file`, `title?`, `tags?` comma-separated, `force?`; auth), `GET /api/docs/{id}?include_chunks=`, `GET /api/chunks/{id}`, `DELETE /api/docs/{id}` (auth).
   - Map `ServiceError` and `IngestError` to `ErrorOut` JSON with the right status (400, 401, 404, 409 for duplicate, 413, 422). A generic 500 handler returns `{"error": "internal"}` with no stack trace.
6. Tests (`moto` for S3):
   - No token and wrong token on write routes give 401; read routes need no token.
   - Upload Markdown, then get it back with tags and chunks.
   - Uploading the same content again returns `duplicate` with the first `doc_id`, and the doc count stays 1.
   - Upload HTML with `KB_BUCKET` set (moto bucket): the object exists at `originals/<id>/...` and `s3_key` is stored.
   - Multipart PDF upload works; image-only PDF gives 422 `no_text_layer`.
   - Delete removes doc, chunks and FTS rows; audit log has upload and delete rows with the token name.

## Do not

- Embed anything or build the index (phase 07).
- Implement near-duplicate checks, update, merge or ingest_url (phase 08).

## Acceptance

- [ ] `pytest -q` passes.
- [ ] Manual check against a local server (`KB_WRITE_TOKENS=dev:devtoken`): `curl` upload of a Markdown file, `GET` it, re-upload gives 409 `duplicate`, `DELETE` it, `GET` gives 404. Record the commands in the phase log notes.
- [ ] Phase log updated; commit `phase 05: core write path`.
