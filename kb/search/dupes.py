"""Near-duplicate detection: compare a new document's vector with every stored doc."""

from __future__ import annotations


import numpy as np

from kb.schemas import DuplicateCandidate
from kb.search.embedder import normalize_rows
from kb.search.state import SearchState
from kb.store import Store

MAX_CANDIDATES = 3


def doc_vector(chunk_vectors: np.ndarray) -> np.ndarray:
    """Normalized mean of a document's chunk vectors (shape `(384,)`)."""
    mean = np.asarray(chunk_vectors, dtype=np.float32).mean(axis=0, keepdims=True)
    return normalize_rows(mean)[0]


def find_near_duplicates(
    conn: Store, state: SearchState, chunk_vectors: np.ndarray
) -> list[DuplicateCandidate]:
    """Existing docs whose vector is close to the new document's, best first.

    The new document's vector is the normalized mean of `chunk_vectors`; each
    stored doc's is the same mean over its indexed chunks (`doc_vectors`, one
    matrix for all docs). A doc is a candidate when the cosine is at least
    `settings.dup_threshold`; at most 3 are returned. Docs without vectors in
    the index cannot be compared and are not reported.
    """
    doc_ids, matrix = state.index.doc_vectors()
    if doc_ids.shape[0] == 0:
        return []
    sims = matrix @ doc_vector(chunk_vectors)
    threshold = state.settings.dup_threshold
    order = [i for i in np.argsort(-sims, kind="stable") if float(sims[i]) >= threshold]
    order = order[:MAX_CANDIDATES]
    if not order:
        return []
    ids = [int(doc_ids[i]) for i in order]
    marks = ",".join("?" * len(ids))
    rows = {
        r["id"]: r
        for r in conn.execute(f"SELECT id, title, source_url FROM docs WHERE id IN ({marks})", ids)
    }
    return [
        DuplicateCandidate(
            doc_id=int(doc_ids[i]),
            title=rows[int(doc_ids[i])]["title"],
            similarity=round(float(sims[i]), 4),
            source_url=rows[int(doc_ids[i])]["source_url"],
        )
        for i in order
        if int(doc_ids[i]) in rows  # a doc deleted since the index snapshot is skipped
    ]
