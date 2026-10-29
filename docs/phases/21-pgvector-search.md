# Phase 21: Search on pgvector and PostgreSQL full-text search

**Days:** Tue Nov 3 to Wed Nov 4 · **Depends on:** 20 · **Where:** local, then AWS · **PLAN.md sections:** Search

## Goal

In PostgreSQL mode, candidates come from the database instead of process memory: cosine top-k from pgvector and keyword candidates from `tsvector`. The reranker, the 2-per-doc cap, confidence, the timeout fallback and the toggle work as before. The eval still passes.

## Read first

- `kb/search/pipeline.py`, `kb/search/index.py`, `kb/search/text.py`, `kb/search/dupes.py`, `kb/search/resolve.py`, `kb/search/state.py`
- `tests/eval/questions.yaml`, `kb/evaluate.py`
- The phase 11 rows in `docs/PROGRESS.md` (tuned cutoffs and latencies)

## Tasks

1. **Candidate sources behind one interface** (`kb/search/candidates.py`): `vector_candidates(query_vec, k, tags)` and `keyword_candidates(terms, k, tags, with_snippet)`. SQLite mode keeps today's code (numpy index, FTS5 `MATCH` and `bm25`). PostgreSQL mode:
   - Vector: `SELECT ..., 1 - (embedding <=> %s) AS cos FROM chunks c JOIN docs d ... WHERE embedding IS NOT NULL [tag filter] ORDER BY embedding <=> %s LIMIT %s`. At this corpus size an exact scan is fast; add `CREATE INDEX ... USING hnsw (embedding vector_cosine_ops)` anyway and record whether the planner uses it (`EXPLAIN ANALYZE`).
   - Keyword: `websearch_to_tsquery('english', %s)` (OR the terms the same way `fts_match` does today), ranked by `ts_rank_cd(tsv, q)`, snippet from `ts_headline` with `<b>` tags. Keyword-only confidence stays `medium` at best.
2. `kb/search/dupes.py` (near-duplicate check) and `kb/search/resolve.py` (gap resolution) use the same interface, so neither needs the numpy index in PostgreSQL mode.
3. In PostgreSQL mode the app does not build the numpy index at startup, so startup is faster and memory no longer grows with the corpus. `/healthz` `index_size` reports the count of chunks with an embedding.
4. The phrase check in `pipeline.py` (`chunks_fts MATCH ... AND rowid = ?`) gets a PostgreSQL version using `tsv @@ phraseto_tsquery(...)` or the same AND query on one id.
5. Tests: run the search, dupes, gap resolution and toggle tests on both store legs with the fakes. Expect keyword scores on a different scale: assertions about order and confidence must hold, raw score values may differ.
6. **Eval gate.** On a local Docker Postgres loaded with `import-sqlite` from the current snapshot and real models: `python -m kb.cli eval tests/eval/questions.yaml` must show hit@5 at 90% or better and every out-of-corpus question `low`. Record hit@1, hit@5, median and max latency next to the phase 11 numbers. If hit@5 drops, compare the candidate lists for the failing questions before touching cutoffs; record any cutoff change as a deviation.
7. Deploy (`postgres` mode). Four searches for demo question 1 through CloudFront: `200 semantic high`. Record latency next to the phase 11 figure (about 2.3 s).

## Do not

- Change the reranker, chunking or embedding model. The point is the same search on a different store.
- Retune cutoffs to hide a candidate bug.

## Acceptance

- [ ] Both store legs pass the search tests.
- [ ] Eval on PostgreSQL: hit@5 at least 90%, out-of-corpus all `low`; numbers recorded.
- [ ] Deployed searches answer `semantic high`; latency recorded.
- [ ] Phase log updated; commit `phase 21: search on pgvector and tsvector`.
