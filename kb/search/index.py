"""In-memory numpy vector index: load from SQLite, add, remove, top-k.

The index is shared by every request thread and is not tied to a database
connection. Reads never lock: the matrix and its two id arrays live in one
immutable snapshot tuple that writers replace in a single assignment (copy on
write), so a search always sees three arrays of the same length.
"""

from __future__ import annotations

import logging
import threading
from typing import Iterable, Sequence

import numpy as np

from kb.store import Store

log = logging.getLogger(__name__)

DIM = 384

_Snapshot = tuple[np.ndarray, np.ndarray, np.ndarray]


def _empty(dim: int) -> _Snapshot:
    return (
        np.zeros((0, dim), dtype=np.float32),
        np.zeros(0, dtype=np.int64),
        np.zeros(0, dtype=np.int64),
    )


class VectorIndex:
    """Cosine-similarity index over L2-normalized float32 chunk vectors."""

    def __init__(self, dim: int = DIM) -> None:
        self.dim = dim
        self._lock = threading.Lock()
        self._snap: _Snapshot = _empty(dim)

    def __len__(self) -> int:
        return int(self._snap[1].shape[0])

    def load_from_db(self, conn: Store, embed_model: str) -> int:
        """Replace the index with every stored vector made by `embed_model`.

        Rows with a NULL embedding, another model, or a BLOB of the wrong size
        are skipped (the backfill re-embeds them). Returns the number loaded.
        """
        rows = conn.execute(
            "SELECT id, doc_id, embedding FROM chunks"
            " WHERE embedding IS NOT NULL AND embed_model = ? ORDER BY id",
            (embed_model,),
        ).fetchall()
        want = self.dim * 4
        vectors, chunk_ids, doc_ids, bad = [], [], [], 0
        for row in rows:
            blob = row["embedding"]
            if len(blob) != want:
                bad += 1
                continue
            vectors.append(np.frombuffer(blob, dtype=np.float32))
            chunk_ids.append(row["id"])
            doc_ids.append(row["doc_id"])
        if bad:
            log.warning("skipped vectors with the wrong size", extra={"count": bad})
        snap = (
            (
                np.stack(vectors).astype(np.float32, copy=False),
                np.asarray(chunk_ids, dtype=np.int64),
                np.asarray(doc_ids, dtype=np.int64),
            )
            if vectors
            else _empty(self.dim)
        )
        with self._lock:
            self._snap = snap
        return len(chunk_ids)

    def add(
        self, chunk_ids: Sequence[int], doc_ids: Sequence[int], vectors: np.ndarray
    ) -> None:
        """Add vectors (rows already L2-normalized). A chunk id already present is replaced."""
        vecs = np.asarray(vectors, dtype=np.float32)
        if vecs.ndim != 2 or vecs.shape[1] != self.dim:
            raise ValueError(f"vectors must have shape (n, {self.dim})")
        if not (len(chunk_ids) == len(doc_ids) == vecs.shape[0]):
            raise ValueError("chunk_ids, doc_ids and vectors must have the same length")
        if vecs.shape[0] == 0:
            return
        new_chunks = np.asarray(chunk_ids, dtype=np.int64)
        new_docs = np.asarray(doc_ids, dtype=np.int64)
        with self._lock:
            matrix, old_chunks, old_docs = self._snap
            keep = ~np.isin(old_chunks, new_chunks)
            self._snap = (
                np.concatenate([matrix[keep], vecs]),
                np.concatenate([old_chunks[keep], new_chunks]),
                np.concatenate([old_docs[keep], new_docs]),
            )

    def replace_doc(
        self, doc_id: int, chunk_ids: Sequence[int], vectors: np.ndarray
    ) -> None:
        """Swap all of a doc's vectors for the given ones in one step.

        Used after an update or merge rewrote the doc's chunks (new chunk ids).
        Readers see either the old vectors or the new ones, never a doc that is
        briefly missing. Empty input just removes the doc's vectors.
        """
        ids = np.asarray(chunk_ids, dtype=np.int64)
        vecs = np.asarray(vectors, dtype=np.float32).reshape(-1, self.dim)
        if ids.shape[0] != vecs.shape[0]:
            raise ValueError("chunk_ids and vectors must have the same length")
        with self._lock:
            matrix, old_chunks, old_docs = self._snap
            keep = (old_docs != doc_id) & ~np.isin(old_chunks, ids)
            self._snap = (
                np.concatenate([matrix[keep], vecs]),
                np.concatenate([old_chunks[keep], ids]),
                np.concatenate([old_docs[keep], np.full(ids.shape[0], doc_id, dtype=np.int64)]),
            )

    def remove_doc(self, doc_id: int) -> int:
        """Drop every vector of `doc_id`; returns how many were removed."""
        with self._lock:
            matrix, chunk_ids, doc_ids = self._snap
            keep = doc_ids != doc_id
            removed = int(keep.size - keep.sum())
            if removed:
                self._snap = (matrix[keep], chunk_ids[keep], doc_ids[keep])
        return removed

    def top_k(
        self, qvec: np.ndarray, k: int, allowed_doc_ids: Iterable[int] | None = None
    ) -> list[tuple[int, int, float]]:
        """Return up to `k` `(chunk_id, doc_id, cosine)` tuples, best first.

        `allowed_doc_ids` restricts candidates to those documents (the tags
        filter); an empty collection matches nothing. Uses `argpartition` to
        find the k winners, then sorts only those.
        """
        matrix, chunk_ids, doc_ids = self._snap
        if k <= 0 or matrix.shape[0] == 0:
            return []
        scores = matrix @ np.asarray(qvec, dtype=np.float32)
        if allowed_doc_ids is not None:
            mask = np.isin(doc_ids, np.fromiter(allowed_doc_ids, dtype=np.int64))
            scores = np.where(mask, scores, -np.inf)
            available = int(mask.sum())
        else:
            available = scores.shape[0]
        k = min(k, available)
        if k <= 0:
            return []
        if k < scores.shape[0]:
            top = np.argpartition(scores, scores.shape[0] - k)[-k:]
        else:
            top = np.arange(scores.shape[0])
        top = top[np.argsort(-scores[top], kind="stable")]
        return [(int(chunk_ids[i]), int(doc_ids[i]), float(scores[i])) for i in top]

    def doc_vector(self, doc_id: int) -> np.ndarray | None:
        """Mean of the doc's chunk vectors, re-normalized; None if the doc has none."""
        matrix, _, doc_ids = self._snap
        mask = doc_ids == doc_id
        if not mask.any():
            return None
        mean = matrix[mask].mean(axis=0)
        norm = float(np.linalg.norm(mean))
        return (mean / norm).astype(np.float32) if norm > 0 else None

    def doc_vectors(self) -> tuple[np.ndarray, np.ndarray]:
        """Mean chunk vector of every doc, re-normalized, computed in one pass.

        Returns `(doc_ids, matrix)`: `doc_ids` has shape `(n,)` and `matrix`
        `(n, dim)`, row `i` belonging to `doc_ids[i]`. Docs whose mean is the
        zero vector are left out. Used by the near-duplicate check, which then
        needs a single matrix product instead of one `doc_vector` call per doc.
        """
        matrix, _, doc_ids = self._snap
        if matrix.shape[0] == 0:
            return np.zeros(0, dtype=np.int64), np.zeros((0, self.dim), dtype=np.float32)
        unique, inverse = np.unique(doc_ids, return_inverse=True)
        sums = np.zeros((unique.shape[0], self.dim), dtype=np.float64)
        np.add.at(sums, inverse, matrix)
        norms = np.linalg.norm(sums, axis=1)
        ok = norms > 0
        return unique[ok], (sums[ok] / norms[ok, None]).astype(np.float32)

    def doc_ids(self) -> set[int]:
        """Ids of the documents that have at least one vector."""
        return {int(d) for d in np.unique(self._snap[2])}
