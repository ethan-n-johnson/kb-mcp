"""Candidate sources for search: where cosine matches and keyword matches come from.

One interface for the pipeline, the near-duplicate check and the evaluator, with
the dialect decided here and in the store modules:

- SQLite: cosine matches come from the in-memory numpy index (`state.index`),
  keyword matches from FTS5 with BM25.
- PostgreSQL: cosine matches come from pgvector (`<=>`, `kb.store.postgres.vector_rows`),
  keyword matches from `tsvector`. The numpy index stays empty.

Every function takes the open store (`conn`) and returns the same shapes in both
dialects, so nothing after retrieval (rerank, the 2-per-doc cap, confidence)
knows which store answered.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from kb.search.embedder import normalize_rows
from kb.search.state import SearchState
from kb.store import Store


def tagged_doc_ids(conn: Store, tags: list[str]) -> list[int]:
    """Ids of the documents that carry every one of `tags`."""
    marks = ",".join("?" * len(tags))
    rows = conn.execute(
        f"SELECT doc_id FROM doc_tags WHERE tag IN ({marks})"
        " GROUP BY doc_id HAVING count(DISTINCT tag) = ?",
        [*tags, len(tags)],
    ).fetchall()
    return [r["doc_id"] for r in rows]


def _embed_model(state: SearchState) -> str:
    assert state.embedder is not None
    return state.embedder.model_name


def vector_candidates(
    conn: Store, state: SearchState, qvec: np.ndarray, k: int, tags: list[str]
) -> list[tuple[int, int, float]]:
    """Up to `k` `(chunk_id, doc_id, cosine)` tuples, best first.

    `tags` keeps only documents that carry all of them. Only vectors made by the
    current embedding model are considered.
    """
    if conn.dialect == "postgres":
        from kb.store import postgres

        blob = np.ascontiguousarray(qvec, dtype=np.float32).tobytes()
        rows = postgres.vector_rows(conn, blob, k, tags, _embed_model(state))  # type: ignore[arg-type]
        return [(int(r["chunk_id"]), int(r["doc_id"]), float(r["cos"])) for r in rows]
    allowed = tagged_doc_ids(conn, tags) if tags else None
    return state.index.top_k(qvec, k, allowed)


def keyword_candidates(
    conn: Store, terms: list[str], k: int, tags: list[str], with_snippet: bool
) -> list[Any]:
    """Top `k` keyword matches, best first (`Store.fts_rows` rows; higher `score` is better)."""
    return conn.fts_rows(terms, tags, k, with_snippet)


def keyword_covers(conn: Store, terms: list[str], chunk_id: int) -> bool:
    """True if the chunk matches every one of `terms` (the store's own tokenizer and stemmer)."""
    return conn.fts_covers(terms, chunk_id)


def doc_vectors(conn: Store, state: SearchState) -> tuple[np.ndarray, np.ndarray]:
    """Mean chunk vector of every document, re-normalized: `(doc_ids, matrix)`.

    `doc_ids` has shape `(n,)` and `matrix` `(n, 384)`, row `i` belonging to
    `doc_ids[i]`. Documents whose mean is the zero vector are left out.
    """
    if conn.dialect == "postgres":
        from kb.store import postgres

        rows = postgres.doc_vector_rows(conn, _embed_model(state))  # type: ignore[arg-type]
        dim = state.index.dim
        usable = [r for r in rows if len(r["v"]) == dim * 4]
        if not usable:
            return np.zeros(0, dtype=np.int64), np.zeros((0, dim), dtype=np.float32)
        ids = np.asarray([r["doc_id"] for r in usable], dtype=np.int64)
        means = np.stack([np.frombuffer(r["v"], dtype=np.float32) for r in usable])
        norms = np.linalg.norm(means, axis=1)
        ok = norms > 0
        return ids[ok], normalize_rows(means[ok])
    return state.index.doc_vectors()
