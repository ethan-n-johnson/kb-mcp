"""Automatic gap resolution: mark unanswered questions as resolved by a new or changed doc.

After a doc is stored, updated or merged into, each recent unresolved question
is scored against only that doc's chunks. A question the doc answers well is
marked resolved (`gaps.resolved_doc_id`). The service calls this after the
result of the write is decided and never lets it raise.
"""

from __future__ import annotations

import logging
import sqlite3

import numpy as np

from kb import db
from kb.search.embedder import passage_text
from kb.search.state import SearchState

log = logging.getLogger(__name__)

MAX_QUERIES = 50


def _open_questions(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """Up to `limit` distinct unresolved `query_norm` values with an example query.

    Most asked first, then most recently asked. The example is the latest row.
    """
    return conn.execute(
        "SELECT g.query_norm, g.query FROM gaps g JOIN ("
        " SELECT max(id) AS id, count(*) AS n FROM gaps WHERE resolved_doc_id IS NULL"
        " GROUP BY query_norm ORDER BY n DESC, max(id) DESC LIMIT ?"
        ") m ON m.id = g.id ORDER BY m.n DESC, g.id DESC",
        (limit,),
    ).fetchall()


def resolve_gaps(conn: sqlite3.Connection, state: SearchState | None, doc_id: int) -> int:
    """Resolve open gaps that `doc_id` answers; returns how many questions were resolved.

    A question counts as answered when the reranker scores its best chunk of
    the doc at or above `KB_CONF_HIGH` (the cutoff for a `high` search
    confidence). With `KB_RERANK=off` there is no reranker, so the best cosine
    against the doc's stored vectors is compared with `KB_COSINE_CONF_HIGH`
    instead, the same substitution the search makes. Does nothing (returns 0)
    when the needed models are missing or unhealthy, or there are no open gaps.
    Model errors propagate; the service wraps this call and logs them.
    """
    if state is None or state.embedder is None or not state.embedder.healthy:
        return 0
    settings = state.settings
    reranker = state.reranker if state.rerank_enabled else None
    if state.rerank_enabled and (reranker is None or not reranker.healthy):
        return 0
    questions = _open_questions(conn, MAX_QUERIES)
    if not questions:
        return 0
    rows = conn.execute(
        "SELECT c.heading_path, c.text, c.embedding, c.embed_model, d.title FROM chunks c"
        " JOIN docs d ON d.id = c.doc_id WHERE c.doc_id = ? ORDER BY c.ord",
        (doc_id,),
    ).fetchall()
    if not rows:
        return 0
    texts = [passage_text(r["title"], r["heading_path"], r["text"]) for r in rows]
    vectors = None
    if reranker is None:
        model = state.embedder.model_name
        usable = [r for r in rows if r["embedding"] is not None and r["embed_model"] == model]
        if not usable:
            return 0
        vectors = np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in usable])
    resolved: list[str] = []
    for question in questions:
        if reranker is not None:
            best = float(np.max(reranker.score(question["query"], texts)))
            threshold = settings.conf_high
        else:
            best = float(np.max(vectors @ state.embedder.embed_query(question["query"])))
            threshold = settings.cosine_conf_high
        # Judge the score as a client would see it (4 decimals), like the search does.
        if round(best, 4) >= threshold:
            resolved.append(question["query_norm"])
    if resolved:
        with db.transaction(conn):
            conn.executemany(
                "UPDATE gaps SET resolved_doc_id = ? WHERE query_norm = ? AND resolved_doc_id IS NULL",
                [(doc_id, norm) for norm in resolved],
            )
        log.info("gaps resolved", extra={"doc_id": doc_id, "resolved": len(resolved)})
    return len(resolved)
