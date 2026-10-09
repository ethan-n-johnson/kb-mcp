# Phase 07: Search pipeline

**Day:** 3 (Fri Oct 9) · **Depends on:** 05 · **Where:** local, then a redeploy · **PLAN.md sections:** Search (all); Architecture (runtime rules); Ingestion step 6

## Goal

Semantic search (bge-small embeddings, in-memory numpy index, FTS5 union, cross-encoder rerank, confidence flag) with automatic keyword fallback, the admin toggle, gap logging and search metrics. Uploads now embed and update the index.

## Read first

- `CLAUDE.md` (runtime rules), `docs/PROGRESS.md`
- `docs/PLAN.md`: "Search" (all of it), "Architecture" runtime rules, step 6 of "Ingestion and data model"

## Tasks

1. `kb/search/embedder.py`: class `Embedder`.
   - `load()` builds `TextEmbedding("BAAI/bge-small-en-v1.5", cache_dir=KB_MODEL_DIR)`; failures set `healthy=False` and log the error, never raise.
   - `embed_passages(texts) -> np.ndarray[float32, (n, 384)]`, L2-normalized.
   - `embed_query(q)` must apply the bge query prefix exactly once: `"Represent this sentence for searching relevant passages: "`. Check whether fastembed's `query_embed` already adds a prefix for this model; write a `models` test that pins the behavior so the prefix is never doubled or missing.
   - Track consecutive failures; after 3 in a row, mark unhealthy until the next successful call.
2. `kb/search/reranker.py`: class `Reranker` wrapping `TextCrossEncoder("Xenova/ms-marco-MiniLM-L-6-v2")`, same health pattern; `score(query, texts) -> np.ndarray` returning the **sigmoid** of the raw scores. A `KB_RERANK=off` env switch (cut line 5) makes the pipeline rank by cosine instead.
3. `kb/search/index.py`: class `VectorIndex`.
   - Holds `matrix (N, 384) float32`, `chunk_ids`, `doc_ids` (numpy arrays); guarded by a `threading.Lock` for mutations; searches read a consistent snapshot of the three references.
   - `load_from_db(conn, embed_model)`: read BLOBs with `np.frombuffer(..., dtype=np.float32)` for rows whose `embed_model` matches.
   - `add(chunk_ids, doc_ids, vectors)`, `remove_doc(doc_id)`, `top_k(qvec, k) -> list[(chunk_id, doc_id, score)]` with `np.argpartition` then a sort of the k winners, `doc_vector(doc_id)` (mean of its chunk vectors, re-normalized; phase 08 uses it), `__len__`.
4. Startup in the FastAPI lifespan (skipped entirely when `KB_MODELS=off`):
   - Load both models in a thread pool.
   - **Backfill:** embed every chunk whose `embedding IS NULL` or whose `embed_model` differs, in batches of 64, and store the BLOBs. This also repairs uploads made while models were down.
   - Build the index from the DB. Log the counts and the time taken.
5. Wire the phase 05 hooks:
   - `_after_chunks_written`: if the embedder is healthy, embed each chunk's text as `"{title} > {heading_path}\n\n{text}"`, store BLOBs and `embed_model` in one transaction, then `index.add`. If not healthy, leave NULL (the next startup backfills).
   - `_after_doc_deleted`: `index.remove_doc`.
6. `kb/search/pipeline.py`: `async search(state, query, k=5, tags=None) -> SearchResult` matching PLAN.md's response shape exactly.
   - Semantic path (models healthy and toggle off): embed query in a thread pool; `top_k(30)`; union with FTS5 top 10 chunk ids; rerank the union (thread pool); keep at most 2 chunks per doc; top `k`. The whole semantic attempt runs under `asyncio.wait_for(..., KB_SEARCH_TIMEOUT)`; a timeout or exception falls through to keyword mode.
   - Tags filter: restrict candidates to docs having **all** given tags (FTS via a join; semantic via a mask on `doc_ids`).
   - Keyword path: build an FTS5 query from the user's words, each quoted as a string (so input like `a-b` or `"` cannot cause FTS syntax errors), joined with `OR`; rank with `bm25(chunks_fts)`; `snippet(chunks_fts, 1, '<b>', '</b>', '...', 24)`.
   - Confidence: semantic per PLAN.md's table using `KB_CONF_HIGH` / `KB_CONF_LOW`; keyword is `medium` if the top hit contains every query term (case-insensitive, after the same tokenization), else `low`; never `high`. No results is `low`.
   - Semantic snippet: the first 240 characters of the chunk text, cut at a word boundary.
   - Results include `verified_at` from the doc.
7. Gap logging: every `low` result inserts into `gaps` with `query`, `query_norm` (lowercase, punctuation stripped, a small English stop-word list removed, whitespace collapsed), `top_score` (null if none), `mode`.
8. Fallback toggle: in-memory flag on `app.state`; `POST /api/admin/fallback` with `{"on": true|false}` (auth, audited). `/healthz` now reports the real `mode` (`semantic` or `keyword`), model statuses (`ok`, `failed`, `off`), `fallback_toggle`, chunk count and index size. Still 200 in keyword mode.
9. `kb/metrics.py`: `emit_search_metrics(mode, confidence, latency_ms)` writes one CloudWatch Embedded Metric Format line to the log: namespace `KbMcp`, dimension `Mode`, metrics `LatencyMs` (Milliseconds), `ConfidenceLevel` (high=2, medium=1, low=0), `KeywordSearches` (1 if keyword, else 0). Phase 15 builds alarms on these.
10. REST: `GET /api/search?q=&k=5&tags=a,b`. Rejects empty `q` (422) and caps `k` at 20.
11. Tests:
    - Fast (models off): keyword search over the fixtures, FTS-hostile input (`"`, `-`, `*`, `AND`) does not error, keyword never returns `high`, `low` writes a gap row, tags filter works.
    - `models`: a semantic query finds the right fixture chunk at rank 1 with `mode: semantic`; the toggle forces keyword; a monkeypatched slow embedder (sleep past the timeout) falls back to keyword; delete removes the doc from results; restarting the app rebuilds the index from the DB with **zero** embed calls (spy on `embed_passages`); backfill embeds NULL rows.

## Do not

- Add MCP tools, near-duplicate checks or the other write operations (phase 08 and 09).
- Store vectors anywhere except SQLite BLOBs and process memory.

## Acceptance

- [ ] `pytest -q` and `pytest -q -m models` both pass.
- [ ] Local server with models on: upload 3 or 4 fixture docs, then `curl 'localhost:8000/api/search?q=...'` returns `mode: semantic`, sensible `heading_path` values, and a `confidence`. Record the latency from the logs.
- [ ] Toggle on, same query returns `mode: keyword`; toggle off returns to semantic.
- [ ] If `kb-app` is running: push a new image (`infra/push-image.sh`), `infra/deploy.sh app <tag>`, and `/healthz` through the ALB shows `mode: semantic` and both models `ok`.
- [ ] Phase log updated; commit `phase 07: search pipeline`.
