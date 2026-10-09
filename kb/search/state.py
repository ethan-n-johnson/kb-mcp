"""Runtime search state: models, vector index and the fallback toggle.

One `SearchState` lives on `app.state.search` (and, from phase 09, in the stdio
MCP process). Models are injected, so tests pass the doubles from
`tests/fakes.py`; production builds the real ones with `create_models`.
The index belongs to the state, never to a database connection.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Protocol, Sequence

import numpy as np

from kb.config import Settings
from kb.search.embedder import Embedder
from kb.search.index import VectorIndex
from kb.search.reranker import Reranker

log = logging.getLogger(__name__)


class EmbedderLike(Protocol):
    """What the pipeline needs from an embedder (`Embedder` and the test fake)."""

    model_name: str

    @property
    def healthy(self) -> bool: ...

    @property
    def status(self) -> str: ...

    def load(self) -> bool: ...

    def embed_passages(self, texts: Sequence[str]) -> np.ndarray: ...

    def embed_query(self, query: str) -> np.ndarray: ...


class RerankerLike(Protocol):
    """What the pipeline needs from a reranker (`Reranker` and the test fake)."""

    model_name: str

    @property
    def healthy(self) -> bool: ...

    @property
    def status(self) -> str: ...

    def load(self) -> bool: ...

    def score(self, query: str, texts: Sequence[str]) -> np.ndarray: ...


@dataclass
class SearchState:
    """Everything a search needs besides the database file.

    `embedder` is None when models are off; `reranker` is None when models are
    off or `KB_RERANK=off` (candidates are then ranked by cosine).
    """

    settings: Settings
    embedder: EmbedderLike | None = None
    reranker: RerankerLike | None = None
    index: VectorIndex = field(default_factory=VectorIndex)
    fallback_toggle: bool = False

    @property
    def db_path(self) -> str:
        """Path of the SQLite file (each search opens its own connection)."""
        return self.settings.db_path

    @property
    def rerank_enabled(self) -> bool:
        """True when candidates are reranked by the cross-encoder."""
        return self.settings.rerank_on

    def semantic_ready(self) -> bool:
        """True when the next search should take the semantic path."""
        if self.fallback_toggle or self.embedder is None or not self.embedder.healthy:
            return False
        if self.rerank_enabled and (self.reranker is None or not self.reranker.healthy):
            return False
        return True

    @property
    def mode(self) -> str:
        """`semantic` or `keyword`, as reported by `/healthz`."""
        return "semantic" if self.semantic_ready() else "keyword"

    def model_status(self) -> dict[str, str]:
        """`ok`, `failed` or `off` for each model."""
        return {
            "embedder": self.embedder.status if self.embedder is not None else "off",
            "reranker": self.reranker.status if self.reranker is not None else "off",
        }


def create_models(settings: Settings) -> tuple[EmbedderLike | None, RerankerLike | None]:
    """Build the (not yet loaded) models for `settings`.

    Returns `(None, None)` when `KB_MODELS=off`, and no reranker when
    `KB_RERANK=off`.

    TEST ONLY: with `KB_FAKE_MODELS=1` the deterministic doubles from
    `tests/fakes.py` replace the real models, so the server can be exercised
    end to end where the ONNX models cannot be downloaded. The Docker image does
    not contain `tests/`, so the variable is inert there; never set it in a
    deployment.
    """
    if not settings.models_on:
        return None, None
    if os.environ.get("KB_FAKE_MODELS") == "1":
        try:
            from tests.fakes import FakeEmbedder, FakeReranker
        except ImportError:
            log.error("KB_FAKE_MODELS=1 but tests.fakes is not importable; using real models")
        else:
            log.warning("KB_FAKE_MODELS=1: using test doubles instead of real models")
            return FakeEmbedder(), (FakeReranker() if settings.rerank_on else None)
    embedder = Embedder(settings.model_dir)
    reranker = Reranker(settings.model_dir) if settings.rerank_on else None
    return embedder, reranker
