# Phase 08: Remaining operations

**Day:** 4 (Sat Oct 10) · **Depends on:** 07 · **Where:** local · **PLAN.md sections:** Ingestion and data model (duplicate rules); Search (gap logging); Interfaces (all rows)

## Goal

Every remaining service operation and its REST route: near-duplicate check, `ingest_url`, `update`, `merge`, `verify`, `stale`, `gaps`, `list_tags`, and automatic gap resolution. After this phase, `service.py` covers all 11 operations and MCP (phase 09) is a thin layer.

## Read first

- `CLAUDE.md`, `docs/PROGRESS.md` (deviation 5)
- `docs/PLAN.md`: duplicate rules, gap logging, the full "Interfaces" table

## Tasks

1. **Near-duplicate check** in `upload` (and therefore `ingest_url`):
   - Restructure `upload` so chunk embeddings are computed **before** the write transaction when the embedder is healthy; the same vectors are then stored, so nothing is embedded twice.
   - Doc vector = normalized mean of its chunk vectors. Compare against `index.doc_vector` for every existing doc (vectorize this: build a doc-vector matrix once per call; fine at about 100 docs).
   - If the best cosine is at least `KB_DUP_THRESHOLD` and `force` is false: write nothing and return `status="possible_duplicate"` with up to 3 `candidates` (`doc_id`, `title`, `similarity`, `source_url`).
   - Models down: skip the check and add `"near_duplicate_check": "skipped: models unavailable"` to the result. The exact-hash check always runs first.
2. **`ingest_url(url, tags, force, actor)`**: `fetch_url` (phase 04), detect the format from the response, then the normal `upload` path with `source_url` set to the final URL. HTML and PDF originals go to S3.
3. **`update(doc_id, content?, title?, tags?, actor)`**: tags-only changes touch `doc_tags` only. Content or title changes re-chunk and re-embed (the embedded text includes the title, so a title change re-embeds too; note this in PROGRESS deviations). Update `content_hash` (409 if it now equals another doc's hash), `updated_at`, replace chunks, update the index after commit, audit.
4. **`merge(target_id, source_ids, delete_sources=True, actor)`**: for each source, append to the target's Markdown a section `## <source title>`, a line `Source: <source_url>` when present, then the source body with every heading demoted one level (cap at H6). Re-chunk and re-embed the target; delete sources if asked; repoint `gaps.resolved_doc_id` from sources to the target; all DB changes in one transaction, index updates after commit; one audit row per affected doc. Reject merging a doc into itself and unknown ids.
5. **`verify(doc_id, note?, actor)`**: set `verified_at` to now (UTC) and `verified_by` to the token name; audit with the note.
6. **`stale(days=90, limit=20)`**: docs whose latest of `verified_at` and `updated_at` (falling back to `created_at`) is older than `days`, oldest first, with `days_since`.
7. **`gaps(limit=20, include_resolved=False)`**: group by `query_norm`; return `query_norm`, an example `query`, `count`, best `top_score`, `last_seen`, `resolved_doc_id`; order by `count` desc, then `last_seen` desc.
8. **`list_tags()`**: tag and doc count, sorted by count desc.
9. **Automatic gap resolution** (deviation 5): after a successful upload, update or merge, if models are healthy, take up to 50 distinct unresolved `query_norm` values, score each example query against only the new doc's chunks with the reranker, and set `resolved_doc_id` when the score is at least `KB_CONF_HIGH`. Run it after the response is decided; failures are logged, never raised.
10. REST routes per PLAN.md: `PATCH /api/docs/{id}`, `POST /api/docs/merge`, `POST /api/ingest`, `GET /api/tags`, `POST /api/docs/{id}/verify`, `GET /api/stale`, `GET /api/gaps`. Writes require the token. `possible_duplicate` returns 200 with the status field (it is a normal outcome the caller acts on), not an error.
11. Tests (fast where possible, `models` for the vector parts):
    - Near-duplicate: a lightly edited copy of a fixture returns `possible_duplicate` with the original as top candidate; `force=true` saves it; with models off the result says the check was skipped.
    - `update`: tags-only does not re-embed (spy); content change replaces chunks and search finds the new text.
    - `merge`: target contains source sections with demoted headings and source URLs; sources gone; search hits the target.
    - `verify` then `stale(days=0)` ordering; a doc backdated 200 days appears in `stale(days=90)`.
    - `gaps` grouping and counts; auto-resolution marks a gap after uploading a doc that answers it.
    - `ingest_url` with `httpx.MockTransport` for a public HTML page.
    - All write routes return 401 without a token and write audit rows with one.

## Do not

- Build MCP tools (phase 09) or UI (phase 13).
- Add multi-instance or locking schemes beyond SQLite transactions.

## Acceptance

- [ ] `pytest -q` and `pytest -q -m models` pass.
- [ ] Every row of PLAN.md's interfaces table has a working REST route and a `service.py` function; list them with their test names in the phase log notes.
- [ ] Phase log updated; commit `phase 08: remaining operations`.
