"""fastembed wrapper for bge-small: passage and query embeddings with a health flag.

Query prefix finding (fastembed 0.9.0, checked in `.venv`): `TextEmbedding.query_embed`
is the base-class implementation, which calls plain `embed`. `OnnxTextEmbedding`, the
class that serves `BAAI/bge-small-en-v1.5`, does not override it, and fastembed's own
model description says prefixes are "not so necessary". Only the multitask models
override `query_embed`. So fastembed adds NO prefix for this model, and we prepend
`QUERY_PREFIX` ourselves exactly once in `Embedder.embed_query`. Passages get no
prefix (bge is trained that way). `tests/test_embedder.py` pins that
`query_embed` is still the base implementation, so a fastembed upgrade that starts
adding its own prefix fails a fast test instead of silently doubling the prefix.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Sequence

import numpy as np

from kb.search.health import HealthTracker

log = logging.getLogger(__name__)

MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIM = 384
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


def passage_text(title: str, heading_path: str, text: str) -> str:
    """The text that is embedded and reranked for a chunk.

    `"{title} > {heading_path}\\n\\n{text}"`. Chunk heading paths usually start
    with the document title already (the H1 or the title fallback), so the
    title is not repeated in that case.
    """
    if not heading_path:
        header = title
    elif heading_path == title or heading_path.startswith(f"{title} > "):
        header = heading_path
    else:
        header = f"{title} > {heading_path}"
    return f"{header}\n\n{text}"


def normalize_rows(vectors: np.ndarray) -> np.ndarray:
    """Return `vectors` as float32 with every row scaled to unit L2 norm."""
    out = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(out, axis=-1, keepdims=True)
    return out / np.maximum(norms, np.float32(1e-12))


class Embedder:
    """Embeds passages and queries with bge-small; never raises from `load()`.

    `model_factory` builds the underlying fastembed model (default
    `fastembed.TextEmbedding`); tests pass a stub. Calls that fail raise to the
    caller after being counted; three failures in a row make `healthy` False
    (see `HealthTracker`).
    """

    model_name = MODEL_NAME
    dim = DIM

    def __init__(
        self,
        cache_dir: str | None = None,
        *,
        model_factory: Callable[..., Any] | None = None,
        health: HealthTracker | None = None,
    ) -> None:
        self._cache_dir = cache_dir
        self._factory = model_factory
        self._model: Any = None
        self._health = health or HealthTracker()

    def load(self) -> bool:
        """Load the model. On failure log it, stay unhealthy and return False."""
        try:
            factory = self._factory
            if factory is None:
                from fastembed import TextEmbedding

                factory = TextEmbedding
            self._model = factory(MODEL_NAME, cache_dir=self._cache_dir)
        except Exception as exc:
            self._health.mark_load_failed()
            log.error(
                "embedder failed to load",
                extra={"model": MODEL_NAME, "error_type": type(exc).__name__, "error": str(exc)[:300]},
            )
            return False
        self._health.mark_loaded()
        return True

    @property
    def healthy(self) -> bool:
        """True when loaded and not tripped by repeated failures."""
        return self._health.healthy

    @property
    def status(self) -> str:
        """`ok`, `failed` or `off`, as reported by `/healthz`."""
        return self._health.status

    def _run(self, call: Callable[[], list[Any]], expected_rows: int) -> np.ndarray:
        """Run a model call, validate the output shape, and keep the health count."""
        if self._model is None:
            raise RuntimeError("embedder is not loaded")
        try:
            rows = call()
            if not rows:
                matrix = np.zeros((0, DIM), dtype=np.float32)
            else:
                matrix = np.stack([np.asarray(r, dtype=np.float32) for r in rows])
            if matrix.shape != (expected_rows, DIM):
                raise ValueError(f"unexpected embedding shape {matrix.shape}")
            result = normalize_rows(matrix)
        except Exception:
            count = self._health.record_failure()
            if count == 3:
                log.error("embedder marked unhealthy after 3 consecutive failures")
            raise
        self._health.record_success()
        return result

    def embed_passages(self, texts: Sequence[str], batch_size: int = 64) -> np.ndarray:
        """Embed passages (no prefix). Returns float32 `(n, 384)`, L2-normalized."""
        items = list(texts)
        if not items:
            return np.zeros((0, DIM), dtype=np.float32)
        return self._run(
            lambda: list(self._model.passage_embed(items, batch_size=batch_size)), len(items)
        )

    def embed_query(self, query: str) -> np.ndarray:
        """Embed one query with the bge query prefix applied exactly once.

        Returns float32 `(384,)`, L2-normalized. The prefix is added here because
        fastembed's `query_embed` adds none for this model (see module docstring).
        """
        text = QUERY_PREFIX + query
        return self._run(lambda: list(self._model.query_embed([text])), 1)[0]
