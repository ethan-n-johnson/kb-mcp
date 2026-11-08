"""Runtime search state: models, vector index and the fallback toggle.

One `SearchState` lives on `app.state.search` (and, from phase 09, in the stdio
MCP process). Models are injected, so tests pass the doubles from
`tests/fakes.py`; production builds the real ones with `create_models`.
The index belongs to the state, never to a database connection. It is used
only with SQLite; in PostgreSQL mode the vectors stay in the database
(`vectors_in_db`) and the index stays empty.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol, Sequence

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

    The keyword-fallback toggle (`fallback_toggle`) is process memory in SQLite
    mode. In PostgreSQL mode it is the `fallback` row of the `settings` table,
    shared by every instance, and writing the property sets the local value at
    once (`service.set_fallback` writes the row). Reading it never waits on the
    database when the process runs a `kb.health.HealthMonitor` (`monitored`,
    the server): the monitor thread reads the row every `TOGGLE_CACHE_SECONDS`
    on its own connection and hands the value over (`apply_toggle`), because a
    read on the event loop would wait for a pooled connection and stall every
    request, `/healthz` included. A process without a monitor (stdio MCP, the
    CLI, tests) reads the row itself, at most every `TOGGLE_CACHE_SECONDS` (by
    `clock`). A failed read keeps the last known value and never raises, so
    `/healthz` and search do not depend on it.

    `semantic_slots()` is the admission control for the semantic path: at most
    `KB_SEMANTIC_CONCURRENCY` semantic computations run at once per process.
    """

    TOGGLE_CACHE_SECONDS = 5.0

    settings: Settings
    embedder: EmbedderLike | None = None
    reranker: RerankerLike | None = None
    index: VectorIndex = field(default_factory=VectorIndex)
    clock: Callable[[], float] = time.monotonic
    monitored: bool = field(default=False, init=False, repr=False)
    # A `kb.search.gap_worker.GapWorker` in server mode with KB_GAP_RESOLUTION=background;
    # None means writes resolve gaps inline (stdio MCP, CLI, service-level tests).
    gap_worker: object | None = field(default=None, init=False, repr=False)
    _toggle: bool = field(default=False, init=False, repr=False)
    _toggle_read_at: float | None = field(default=None, init=False, repr=False)
    _toggle_error_logged: bool = field(default=False, init=False, repr=False)
    _slots: asyncio.Semaphore | None = field(default=None, init=False, repr=False)
    _slots_loop: asyncio.AbstractEventLoop | None = field(default=None, init=False, repr=False)

    @property
    def fallback_toggle(self) -> bool:
        """True while searches are forced to keyword mode (see the class docstring)."""
        if self.settings.dialect == "postgres" and not self.monitored:
            now = self.clock()
            read_at = self._toggle_read_at
            if read_at is None or now - read_at >= self.TOGGLE_CACHE_SECONDS:
                self._toggle_read_at = now  # also after a failure: retry in 5 s, not on every call
                self._refresh_toggle()
        return self._toggle

    @fallback_toggle.setter
    def fallback_toggle(self, on: bool) -> None:
        self._toggle = on
        if self.settings.dialect == "postgres":
            self._toggle_read_at = self.clock()

    def _refresh_toggle(self) -> None:
        from kb.store import open_store, pg_state

        try:
            store = open_store(self.settings)
            try:
                value = pg_state.get_setting(store, "fallback")
            finally:
                store.close()
        except Exception as exc:
            self.toggle_read_failed(exc)
            return
        self.apply_toggle(value)

    def apply_toggle(self, value: str | None) -> None:
        """Take the `fallback` row's value (None: no row, so off) as read from the database."""
        self._toggle_error_logged = False
        self._toggle = value == "on"

    def toggle_read_failed(self, exc: Exception) -> None:
        """Keep the last known toggle after a failed read; log the first failure of a run."""
        if not self._toggle_error_logged:
            log.warning(
                "cannot read the fallback toggle; keeping the last value",
                extra={"error": type(exc).__name__},
            )
            self._toggle_error_logged = True

    def semantic_slots(self) -> asyncio.Semaphore:
        """The semaphore that bounds concurrent semantic computations (call on the event loop).

        Created on first use inside the running loop, so it belongs to that
        loop; a different loop (tests run several) gets a fresh one.
        """
        loop = asyncio.get_running_loop()
        if self._slots is None or self._slots_loop is not loop:
            self._slots = asyncio.Semaphore(self.settings.semantic_concurrency)
            self._slots_loop = loop
        return self._slots

    @property
    def db_path(self) -> str:
        """Path of the SQLite file (each search opens its own connection)."""
        return self.settings.db_path

    @property
    def vectors_in_db(self) -> bool:
        """True when cosine search runs in the database (PostgreSQL), not in `index`."""
        return self.settings.dialect == "postgres"

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
