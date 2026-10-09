"""Cross-encoder reranker (ms-marco MiniLM) returning sigmoid scores in (0, 1)."""

from __future__ import annotations

import logging
from typing import Any, Callable, Sequence

import numpy as np

from kb.search.health import HealthTracker

log = logging.getLogger(__name__)

MODEL_NAME = "Xenova/ms-marco-MiniLM-L-6-v2"


def sigmoid(raw: np.ndarray) -> np.ndarray:
    """Numerically stable logistic function, float32."""
    x = np.asarray(raw, dtype=np.float64)
    return (0.5 * (1.0 + np.tanh(x / 2.0))).astype(np.float32)


class Reranker:
    """Scores (query, passage) pairs with a cross-encoder; same health pattern as `Embedder`."""

    model_name = MODEL_NAME

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
                from fastembed.rerank.cross_encoder import TextCrossEncoder

                factory = TextCrossEncoder
            self._model = factory(MODEL_NAME, cache_dir=self._cache_dir)
        except Exception as exc:
            self._health.mark_load_failed()
            log.error(
                "reranker failed to load",
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

    def score(self, query: str, texts: Sequence[str]) -> np.ndarray:
        """Return the sigmoid of the raw cross-encoder score for each text.

        Output is float32 with one value in (0, 1) per input, in input order.
        """
        items = list(texts)
        if not items:
            return np.zeros(0, dtype=np.float32)
        if self._model is None:
            raise RuntimeError("reranker is not loaded")
        try:
            raw = np.fromiter((float(s) for s in self._model.rerank(query, items)), dtype=np.float64)
            if raw.shape != (len(items),):
                raise ValueError(f"expected {len(items)} scores, got {raw.shape[0]}")
            result = sigmoid(raw)
        except Exception:
            count = self._health.record_failure()
            if count == 3:
                log.error("reranker marked unhealthy after 3 consecutive failures")
            raise
        self._health.record_success()
        return result
