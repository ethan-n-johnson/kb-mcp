"""Keeping vectors in step with the database: embed on write, backfill, build the index."""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Sequence

import numpy as np
from starlette.concurrency import run_in_threadpool

from kb import db
from kb.config import Settings
from kb.ingest.chunk import Chunk
from kb.search.embedder import passage_text
from kb.search.state import EmbedderLike, RerankerLike, SearchState, create_models

log = logging.getLogger(__name__)

BACKFILL_BATCH = 64


def build_state(
    settings: Settings,
    embedder: EmbedderLike | None = None,
    reranker: RerankerLike | None = None,
) -> SearchState:
    """Create a `SearchState`; injected models replace the real ones (tests).

    With `KB_MODELS=off` there are no models at all. With `KB_RERANK=off` the
    reranker is dropped even if one was injected.
    """
    if not settings.models_on:
        return SearchState(settings)
    if embedder is None:
        embedder, built_reranker = create_models(settings)
        reranker = reranker or built_reranker
    return SearchState(settings, embedder, reranker if settings.rerank_on else None)


def _store_vectors(
    conn: sqlite3.Connection, state: SearchState, rows: list[sqlite3.Row], vectors: np.ndarray
) -> list[tuple[int, int, np.ndarray]]:
    """Write `vectors` for `rows` in one transaction; return the rows that still existed."""
    model = state.embedder.model_name  # type: ignore[union-attr]
    stored: list[tuple[int, int, np.ndarray]] = []
    with db.transaction(conn):
        for row, vec in zip(rows, vectors):
            cur = conn.execute(
                "UPDATE chunks SET embedding = ?, embed_model = ? WHERE id = ?",
                (np.ascontiguousarray(vec, dtype=np.float32).tobytes(), model, row["id"]),
            )
            if cur.rowcount:  # the chunk may have been deleted since it was read
                stored.append((row["id"], row["doc_id"], vec))
    return stored


def embed_chunks(state: SearchState | None, title: str, chunks: Sequence[Chunk]) -> np.ndarray | None:
    """Embed not-yet-stored chunks of a doc titled `title`; None when that is not possible.

    Used before a write transaction (upload, update, merge) so the vectors are
    stored together with the chunks and nothing is embedded twice. Returns an
    `(n, 384)` array in chunk order, or None when there is no healthy embedder
    or the embedder raised (the failure is logged; the caller then stores the
    chunks without vectors and the next backfill embeds them).
    """
    embedder = None if state is None else state.embedder
    if embedder is None or not embedder.healthy or not chunks:
        return None
    try:
        return embedder.embed_passages(
            [passage_text(title, c.heading_path, c.text) for c in chunks]
        )
    except Exception:
        log.error("embedding before store failed; chunks left for backfill", exc_info=True)
        return None


def publish_doc(conn: sqlite3.Connection, state: SearchState, doc_id: int) -> int:
    """Make the index hold exactly the doc's stored vectors (current model only).

    Replaces whatever the index had for the doc in one step, so an update that
    rewrote the chunks never leaves stale chunk ids behind. A doc that no longer
    exists is removed from the index. Returns the number of vectors published.
    """
    embedder = state.embedder
    if embedder is None:
        return 0
    rows = conn.execute(
        "SELECT id, embedding FROM chunks"
        " WHERE doc_id = ? AND embedding IS NOT NULL AND embed_model = ? ORDER BY ord",
        (doc_id, embedder.model_name),
    ).fetchall()
    want = state.index.dim * 4
    rows = [r for r in rows if len(r["embedding"]) == want]
    vectors = (
        np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
        if rows
        else np.zeros((0, state.index.dim), dtype=np.float32)
    )
    state.index.replace_doc(doc_id, [r["id"] for r in rows], vectors)
    # A delete that committed between the write and the publish has already run
    # `remove_doc`; make sure its vectors do not linger.
    if conn.execute("SELECT 1 FROM docs WHERE id = ?", (doc_id,)).fetchone() is None:
        state.index.remove_doc(doc_id)
        return 0
    return len(rows)


def backfill(conn: sqlite3.Connection, state: SearchState, batch_size: int = BACKFILL_BATCH) -> int:
    """Embed every chunk whose vector is missing or made by another model.

    Works in batches (one transaction each) and returns the number embedded.
    This also repairs uploads made while the models were down. Stops early, with
    a warning, if the embedder fails.
    """
    embedder = state.embedder
    if embedder is None or not embedder.healthy:
        return 0
    total = 0
    last_id = 0
    while True:
        rows = conn.execute(
            "SELECT c.id, c.doc_id, c.heading_path, c.text, d.title FROM chunks c"
            " JOIN docs d ON d.id = c.doc_id"
            " WHERE c.id > ? AND (c.embedding IS NULL OR c.embed_model IS NOT ?)"
            " ORDER BY c.id LIMIT ?",
            (last_id, embedder.model_name, batch_size),
        ).fetchall()
        if not rows:
            return total
        last_id = rows[-1]["id"]
        try:
            vectors = embedder.embed_passages(
                [passage_text(r["title"], r["heading_path"], r["text"]) for r in rows]
            )
        except Exception:
            log.exception("backfill stopped: embedding failed", extra={"embedded": total})
            return total
        total += len(_store_vectors(conn, state, rows, vectors))


def _start_sync(state: SearchState) -> None:
    """Load the models, backfill missing vectors and build the index (blocking)."""
    started = time.perf_counter()
    for model in (state.embedder, state.reranker):
        if model is not None:
            model.load()
    loaded_at = time.perf_counter()
    conn = db.connect(state.db_path)
    try:
        backfilled = backfill(conn, state)
        indexed = 0
        if state.embedder is not None and state.embedder.healthy:
            indexed = state.index.load_from_db(conn, state.embedder.model_name)
        chunks = int(conn.execute("SELECT count(*) FROM chunks").fetchone()[0])
    finally:
        conn.close()
    log.info(
        "search startup complete",
        extra={
            "mode": state.mode,
            "models": state.model_status(),
            "chunks": chunks,
            "backfilled": backfilled,
            "indexed": indexed,
            "load_seconds": round(loaded_at - started, 2),
            "total_seconds": round(time.perf_counter() - started, 2),
        },
    )


async def start(state: SearchState) -> None:
    """Startup work for the FastAPI lifespan, run in a thread pool.

    Never raises for model problems: a model that fails to load leaves the
    server in keyword mode. Skipped entirely when models are off.
    """
    if state.embedder is None:
        log.info("models off; search is keyword only")
        return
    try:
        await run_in_threadpool(_start_sync, state)
    except Exception:
        log.exception("search startup failed; continuing in keyword mode")
