"""Deterministic test doubles for the embedder and reranker.

The doubles implement the same surface as the production classes
(`kb.search.embedder.Embedder`, `kb.search.reranker.Reranker`, see the
`EmbedderLike` and `RerankerLike` protocols in `kb.search.state`):

- `FakeEmbedder`: `model_name`, `healthy`, `status`, `load()`,
  `embed_passages(texts) -> (n, 384)` and `embed_query(q) -> (384,)`, float32,
  L2-normalized.
- `FakeReranker`: `model_name`, `healthy`, `status`, `load()` and
  `score(query, texts) -> np.ndarray` of floats in (0, 1).

Embeddings are hashed bags of words, so texts sharing words have higher cosine
similarity than unrelated texts. Hashing uses blake2b, not `hash()`, so results
are stable across processes.

Behavior switches for tests: `delay` (seconds to sleep inside each model call,
for the timeout test), `fail` (raise `RuntimeError` from each call, for the
error-fallback test), `healthy` (settable), and call recorders
(`passage_calls`, `query_calls`, `score_calls`) for spying on embed counts.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from typing import Sequence

import numpy as np

DIM = 384
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens of `text`."""
    return _TOKEN_RE.findall(text.lower())


def _bucket(token: str) -> tuple[int, float]:
    """Map a token to (dimension index, sign) deterministically."""
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    return value % DIM, 1.0 if (value >> 40) & 1 else -1.0


class _FakeModel:
    """Shared switches of the fake models."""

    healthy: bool
    delay: float
    fail: bool

    def _enter(self) -> None:
        """Common start of a model call: optional sleep, optional failure."""
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise RuntimeError(f"{type(self).__name__} was told to fail")

    @property
    def status(self) -> str:
        """`ok` while healthy, else `failed` (the real classes report the same words)."""
        return "ok" if self.healthy else "failed"

    def load(self) -> bool:
        """Pretend to load; returns whether the model is healthy."""
        return self.healthy


class FakeEmbedder(_FakeModel):
    """Hashed bag-of-words embedder producing L2-normalized float32 vectors."""

    dim = DIM
    model_name = "fake-bow-384"

    def __init__(self, *, delay: float = 0.0, fail: bool = False, healthy: bool = True) -> None:
        self.delay = delay
        self.fail = fail
        self.healthy = healthy
        self.passage_calls: list[list[str]] = []
        self.query_calls: list[str] = []

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        """Embed `texts` into an array of shape (len(texts), 384), float32, unit norm.

        A text with no tokens maps to a fixed unit vector so norms stay 1.
        """
        out = np.zeros((len(texts), DIM), dtype=np.float32)
        for row, text in enumerate(texts):
            for token in tokenize(text):
                idx, sign = _bucket(token)
                out[row, idx] += sign
            norm = float(np.linalg.norm(out[row]))
            if norm == 0.0:
                out[row, 0] = 1.0
            else:
                out[row] /= norm
        return out

    def embed_passages(self, texts: Sequence[str], batch_size: int = 64) -> np.ndarray:
        """Embed passages; shape (n, 384). Records the call in `passage_calls`."""
        self.passage_calls.append(list(texts))
        self._enter()
        return self.embed(texts)

    def embed_query(self, query: str) -> np.ndarray:
        """Embed one query; shape (384,). Records the call in `query_calls`."""
        self.query_calls.append(query)
        self._enter()
        return self.embed([query])[0]

    @property
    def passages_embedded(self) -> int:
        """Total number of passages embedded so far."""
        return sum(len(batch) for batch in self.passage_calls)


class FakeReranker(_FakeModel):
    """Scores passages by query-token overlap passed through a sigmoid."""

    model_name = "fake-overlap-reranker"

    def __init__(
        self,
        steepness: float = 8.0,
        midpoint: float = 0.4,
        *,
        delay: float = 0.0,
        fail: bool = False,
        healthy: bool = True,
    ) -> None:
        self.steepness = steepness
        self.midpoint = midpoint
        self.delay = delay
        self.fail = fail
        self.healthy = healthy
        self.score_calls: list[tuple[str, list[str]]] = []

    def score(self, query: str, texts: Sequence[str]) -> np.ndarray:
        """Return one float32 score in (0, 1) per text, in order.

        The raw value is the fraction of distinct query tokens present in the
        text; the sigmoid centers it at `midpoint`.
        """
        self.score_calls.append((query, list(texts)))
        self._enter()
        q = set(tokenize(query))
        scores: list[float] = []
        for passage in texts:
            overlap = len(q & set(tokenize(passage))) / len(q) if q else 0.0
            scores.append(1.0 / (1.0 + math.exp(-self.steepness * (overlap - self.midpoint))))
        return np.asarray(scores, dtype=np.float32)
