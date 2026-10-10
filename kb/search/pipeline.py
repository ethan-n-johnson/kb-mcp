"""Search pipeline: semantic path with rerank, keyword fallback, confidence, gaps.

Semantic path (models healthy, toggle off), all under `KB_SEARCH_TIMEOUT`:
embed the query, take the top `KB_RERANK_CANDIDATES` cosine matches (15), union
the FTS5 top 5, rerank the union with the cross-encoder (each passage cut to
`KB_RERANK_MAX_CHARS`, 1000 characters), keep at most 2 chunks per document and
return the top `k`. Without the reranker the top 30 cosine matches are ranked
directly. The smaller rerank pool and the cut passages came from phase 11: on
the t3.medium, reranking 40 full chunks took longer than the 3 second timeout,
so every search fell back to keyword. A timeout or any exception falls through to the keyword path
(FTS5 + BM25). With `KB_RERANK=off` there is no union and no cross-encoder:
candidates are ranked by cosine and judged against the cosine cutoffs.

Model and database work runs in worker threads via `run_in_threadpool`. When
the timeout cancels the awaiting task the thread cannot be interrupted: it
finishes in the background (holding one pool slot) and its result is discarded,
while the keyword fallback answers immediately. `tests/test_search.py` pins that
the fallback does not wait for the slow model.

Each search opens its own SQLite connection(s) inside the worker threads, so it
never shares a connection with a request or with another search.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Callable, Iterable, TypeVar

import numpy as np
from starlette.concurrency import run_in_threadpool

from kb import db, metrics
from kb.schemas import SearchHit, SearchResult
from kb.search.embedder import passage_text
from kb.search.state import SearchState
from kb.search.text import fts_match, normalize_query, query_terms, snippet_from

log = logging.getLogger(__name__)

MAX_K = 20
SEMANTIC_CANDIDATES = 30
FTS_CANDIDATES = 5
MAX_PER_DOC = 2

T = TypeVar("T")


@dataclass
class _Candidate:
    """A chunk under consideration, with the fields needed to build a hit."""

    chunk_id: int
    doc_id: int
    title: str
    heading_path: str
    text: str
    source_url: str | None
    verified_at: str | None
    cosine: float | None = None
    score: float = 0.0
    snippet: str = ""

    def hit(self) -> SearchHit:
        return SearchHit(
            doc_id=self.doc_id,
            chunk_id=self.chunk_id,
            title=self.title,
            heading_path=self.heading_path,
            snippet=self.snippet or snippet_from(self.text),
            score=round(float(self.score), 4),
            source_url=self.source_url,
            verified_at=self.verified_at,
        )


async def _in_thread(fn: Callable[..., T], *args: object) -> T:
    """Run `fn(*args)` in the thread pool."""
    return await run_in_threadpool(fn, *args)


# --- SQL helpers ----------------------------------------------------------------


def _tag_filter_sql(tags: list[str]) -> tuple[str, list[object]]:
    """SQL fragment (on `c.doc_id`) keeping only docs that carry ALL `tags`."""
    marks = ",".join("?" * len(tags))
    sql = (
        " AND c.doc_id IN (SELECT doc_id FROM doc_tags WHERE tag IN (" + marks + ")"
        " GROUP BY doc_id HAVING count(DISTINCT tag) = ?)"
    )
    return sql, [*tags, len(tags)]


def _tagged_doc_ids(conn: sqlite3.Connection, tags: list[str]) -> list[int]:
    marks = ",".join("?" * len(tags))
    rows = conn.execute(
        f"SELECT doc_id FROM doc_tags WHERE tag IN ({marks})"
        " GROUP BY doc_id HAVING count(DISTINCT tag) = ?",
        [*tags, len(tags)],
    ).fetchall()
    return [r["doc_id"] for r in rows]


def _fts_rows(
    conn: sqlite3.Connection, match: str, tags: list[str], limit: int, with_snippet: bool
) -> list[sqlite3.Row]:
    """Top FTS5 rows by BM25 (best first) for a prepared MATCH expression."""
    if not match:
        return []
    tag_sql, tag_params = _tag_filter_sql(tags) if tags else ("", [])
    snippet_col = (
        "snippet(chunks_fts, 1, '<b>', '</b>', '...', 24)" if with_snippet else "''"
    )
    return conn.execute(
        f"SELECT c.id AS chunk_id, c.doc_id, c.heading_path, c.text, d.title, d.source_url,"
        f" d.verified_at, bm25(chunks_fts) AS bm, {snippet_col} AS snip"
        " FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid"
        " JOIN docs d ON d.id = c.doc_id"
        f" WHERE chunks_fts MATCH ?{tag_sql} ORDER BY bm LIMIT ?",
        [match, *tag_params, limit],
    ).fetchall()


def _candidate(row: sqlite3.Row) -> _Candidate:
    return _Candidate(
        chunk_id=row["chunk_id"],
        doc_id=row["doc_id"],
        title=row["title"],
        heading_path=row["heading_path"],
        text=row["text"],
        source_url=row["source_url"],
        verified_at=row["verified_at"],
    )


def _hydrate(conn: sqlite3.Connection, chunk_ids: list[int]) -> dict[int, _Candidate]:
    """Load candidate rows by chunk id; ids no longer in the database are dropped."""
    if not chunk_ids:
        return {}
    marks = ",".join("?" * len(chunk_ids))
    rows = conn.execute(
        "SELECT c.id AS chunk_id, c.doc_id, c.heading_path, c.text, d.title, d.source_url,"
        " d.verified_at FROM chunks c JOIN docs d ON d.id = c.doc_id"
        f" WHERE c.id IN ({marks})",
        chunk_ids,
    ).fetchall()
    return {r["chunk_id"]: _candidate(r) for r in rows}


# --- ranking and confidence -----------------------------------------------------


def _cap_per_doc(ranked: Iterable[_Candidate], k: int) -> list[_Candidate]:
    """Keep the best `k` candidates with at most `MAX_PER_DOC` per document."""
    per_doc: dict[int, int] = {}
    kept: list[_Candidate] = []
    for cand in ranked:
        if per_doc.get(cand.doc_id, 0) >= MAX_PER_DOC:
            continue
        per_doc[cand.doc_id] = per_doc.get(cand.doc_id, 0) + 1
        kept.append(cand)
        if len(kept) == k:
            break
    return kept


def semantic_confidence(top_score: float | None, high: float, low: float) -> str:
    """`high` at or above `high`, `medium` from `low` up to `high`, else `low`."""
    if top_score is None or top_score < low:
        return "low"
    return "high" if top_score >= high else "medium"


# --- semantic path --------------------------------------------------------------


def _retrieve(
    state: SearchState, query: str, qvec: np.ndarray, tags: list[str]
) -> list[_Candidate]:
    """Top cosine matches (plus, when reranking, the FTS top 5) as candidates."""
    conn = db.connect(state.db_path)
    try:
        allowed = _tagged_doc_ids(conn, tags) if tags else None
        pool = SEMANTIC_CANDIDATES
        if state.rerank_enabled:
            pool = min(pool, state.settings.rerank_candidates)
        hits = state.index.top_k(qvec, pool, allowed)
        cosines = {chunk_id: score for chunk_id, _, score in hits}
        ids = [chunk_id for chunk_id, _, _ in hits]
        if state.rerank_enabled:
            match = fts_match(query_terms(query))
            for row in _fts_rows(conn, match, tags, FTS_CANDIDATES, with_snippet=False):
                if row["chunk_id"] not in ids:
                    ids.append(row["chunk_id"])
        loaded = _hydrate(conn, ids)
    finally:
        conn.close()
    out: list[_Candidate] = []
    for chunk_id in ids:
        cand = loaded.get(chunk_id)
        if cand is not None:
            cand.cosine = cosines.get(chunk_id)
            out.append(cand)
    return out


async def _semantic(state: SearchState, query: str, k: int, tags: list[str]) -> SearchResult:
    """The whole semantic attempt; the caller wraps it in the timeout."""
    assert state.embedder is not None
    qvec = await _in_thread(state.embedder.embed_query, query)
    candidates = await _in_thread(_retrieve, state, query, qvec, tags)
    settings = state.settings
    if state.rerank_enabled:
        assert state.reranker is not None
        reranker = state.reranker
        limit = settings.rerank_max_chars
        texts = [passage_text(c.title, c.heading_path, c.text)[:limit] for c in candidates]
        scores = await _in_thread(reranker.score, query, texts) if candidates else []
        for cand, score in zip(candidates, scores):
            cand.score = float(score)
        high, low = settings.conf_high, settings.conf_low
    else:
        for cand in candidates:
            cand.score = float(cand.cosine or 0.0)
        high, low = settings.cosine_conf_high, settings.cosine_conf_low
    candidates.sort(key=lambda c: (-c.score, -(c.cosine or 0.0), c.chunk_id))
    kept = _cap_per_doc(candidates, k)
    # Judge the score as shown (4 decimals), so float32 noise cannot flip a boundary.
    confidence = semantic_confidence(round(kept[0].score, 4) if kept else None, high, low)
    return SearchResult(mode="semantic", confidence=confidence, results=[c.hit() for c in kept])


# --- keyword path ---------------------------------------------------------------


def _covers_all_terms(conn: sqlite3.Connection, terms: list[str], chunk_id: int) -> bool:
    """True if the chunk matches every term, using FTS5's own tokenizer and stemmer."""
    row = conn.execute(
        "SELECT 1 FROM chunks_fts WHERE chunks_fts MATCH ? AND rowid = ?",
        (fts_match(terms, "AND"), chunk_id),
    ).fetchone()
    return row is not None


def _keyword(state: SearchState, query: str, k: int, tags: list[str]) -> SearchResult:
    """FTS5 MATCH with BM25 ranking. Confidence is `medium` at best, never `high`."""
    terms = query_terms(query)
    conn = db.connect(state.db_path)
    try:
        rows = _fts_rows(conn, fts_match(terms), tags, max(k * 4, 20), with_snippet=True)
        cands: list[_Candidate] = []
        for row in rows:
            cand = _candidate(row)
            cand.score = -float(row["bm"])  # bm25() is lower-is-better and negative
            cand.snippet = row["snip"]
            cands.append(cand)
        kept = _cap_per_doc(cands, k)
        confidence = "low"
        if kept and _covers_all_terms(conn, terms, kept[0].chunk_id):
            confidence = "medium"
    finally:
        conn.close()
    return SearchResult(mode="keyword", confidence=confidence, results=[c.hit() for c in kept])


# --- gaps -----------------------------------------------------------------------


def _log_gap(state: SearchState, query: str, result: SearchResult) -> None:
    """Insert a `gaps` row for a low-confidence search; failures are logged only."""
    top = result.results[0].score if result.results else None
    try:
        conn = db.connect(state.db_path)
        try:
            with db.transaction(conn):
                conn.execute(
                    "INSERT INTO gaps (query, query_norm, top_score, mode, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (query, normalize_query(query), top, result.mode, db.utcnow()),
                )
        finally:
            conn.close()
    except Exception:
        log.exception("could not record a search gap")


# --- entry point ----------------------------------------------------------------


async def search(
    state: SearchState, query: str, k: int = 5, tags: list[str] | None = None
) -> SearchResult:
    """Search the knowledge base; the response says which mode answered.

    `k` is clamped to 1..20. `tags` keeps only documents that carry all of
    them. Semantic search is tried first when `state.semantic_ready()`; a
    timeout (`KB_SEARCH_TIMEOUT`) or any error falls back to keyword search.
    Low-confidence results are recorded in `gaps`, and one metrics line is
    logged. The caller validates that `query` is not empty.
    """
    started = time.perf_counter()
    k = max(1, min(int(k), MAX_K))
    wanted = list(dict.fromkeys(t.strip() for t in (tags or []) if t.strip()))
    result: SearchResult | None = None
    if state.semantic_ready():
        try:
            result = await asyncio.wait_for(
                _semantic(state, query, k, wanted), state.settings.search_timeout
            )
        except asyncio.TimeoutError:
            log.warning(
                "semantic search timed out; using keyword search",
                extra={"timeout_s": state.settings.search_timeout},
            )
        except Exception:
            log.exception("semantic search failed; using keyword search")
    if result is None:
        result = await _in_thread(_keyword, state, query, k, wanted)
    if result.confidence == "low":
        await _in_thread(_log_gap, state, query, result)
    latency_ms = (time.perf_counter() - started) * 1000.0
    metrics.emit_search_metrics(result.mode, result.confidence, latency_ms)
    log.info(
        "search",
        extra={
            "mode": result.mode,
            "confidence": result.confidence,
            "results": len(result.results),
            "latency_ms": round(latency_ms, 1),
        },
    )
    return result
