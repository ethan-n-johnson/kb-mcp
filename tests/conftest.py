"""Shared fixtures: temp database and a TestClient with models off."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, ContextManager, Iterator

import pytest
from fastapi.testclient import TestClient

from kb import config, db, ratelimit
from kb.config import Settings
from kb.search import indexing
from kb.search.state import SearchState
from tests.fakes import FakeEmbedder, FakeReranker


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Path of a not-yet-created database file inside a temp directory."""
    return tmp_path / "data" / "kb.db"


@pytest.fixture
def conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    """A connection to a fresh temp database with the schema applied."""
    c = db.connect(db_path)
    db.init_schema(c)
    yield c
    c.close()


@pytest.fixture
def client(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """TestClient with the app lifespan running against a temp DB, models off.

    Builds a fresh app per test: the MCP session manager can run only once per
    instance, so the module-level `kb.main.app` cannot be reused across tests.
    """
    monkeypatch.setenv("KB_DB", str(db_path))
    monkeypatch.setenv("KB_MODELS", "off")
    monkeypatch.setenv("KB_WRITE_TOKENS", "test:testtoken")
    monkeypatch.delenv("KB_BUCKET", raising=False)
    config.clear_token_cache()
    from kb.main import create_app

    with TestClient(create_app()) as c:
        yield c
    config.clear_token_cache()


@pytest.fixture(autouse=True)
def _no_real_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make real AWS calls impossible: fake credentials, no metadata lookups, no bucket.

    S3 tests opt in with `moto.mock_aws` and set `KB_BUCKET` themselves.
    """
    monkeypatch.delenv("KB_BUCKET", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


@pytest.fixture(autouse=True)
def _fresh_rate_limiter() -> Iterator[None]:
    """Clear the search rate limiter before and after each test.

    The limiter is process-wide state (one worker in production), and every
    test client has the same address, so searches from earlier tests would
    otherwise count against later ones.
    """
    ratelimit.reset()
    yield
    ratelimit.reset()


AUTH = {"Authorization": "Bearer testtoken"}

OpenApp = Callable[..., ContextManager[TestClient]]


@pytest.fixture
def open_app(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> OpenApp:
    """Factory for app instances on the temp database, usable several times per test.

    `with open_app() as client:` runs the real lifespan (model load, backfill,
    index build) with `FakeEmbedder` and `FakeReranker` injected. Pass
    `embedder=` or `reranker=` to inject others, `models=False` for `KB_MODELS=off`,
    and extra keyword arguments become environment variables
    (`KB_SEARCH_TIMEOUT=0.3`). `client.app.state.search` is the `SearchState`.

    The near-duplicate cutoff is set explicitly to 1.5 (above any cosine, so
    the check never fires) because the fake embedder's similarities say nothing
    about bge's and many tests upload similar generated documents. Tests of the
    check itself pass `KB_DUP_THRESHOLD=<value>`. The confidence cutoffs are
    pinned to 0.70 and 0.30 for the same reason (pass `KB_CONF_HIGH` or
    `KB_CONF_LOW` to override).
    """
    monkeypatch.setenv("KB_DB", str(db_path))
    monkeypatch.setenv("KB_WRITE_TOKENS", "test:testtoken")
    monkeypatch.delenv("KB_BUCKET", raising=False)
    monkeypatch.delenv("KB_FAKE_MODELS", raising=False)
    monkeypatch.delenv("KB_RERANK", raising=False)
    config.clear_token_cache()

    @contextmanager
    def _open(
        embedder: FakeEmbedder | None = None,
        reranker: FakeReranker | None = None,
        *,
        models: bool = True,
        **env: object,
    ) -> Iterator[TestClient]:
        from kb.main import create_app

        monkeypatch.setenv("KB_MODELS", "on" if models else "off")
        monkeypatch.setenv("KB_DUP_THRESHOLD", "1.5")
        # The fake reranker's scores say nothing about the real one's either, so the
        # confidence cutoffs the tests were written against are pinned here; the
        # defaults in kb/config.py are tuned for the real models (phase 11).
        monkeypatch.setenv("KB_CONF_HIGH", "0.70")
        monkeypatch.setenv("KB_CONF_LOW", "0.30")
        for name, value in env.items():
            monkeypatch.setenv(name, str(value))
        app = create_app(
            embedder or FakeEmbedder(),
            reranker or (FakeReranker() if env.get("KB_RERANK") != "off" else None),
        )
        with TestClient(app) as c:
            yield c

    yield _open
    config.clear_token_cache()


@dataclass
class Svc:
    """Service-level test environment: a connection and a started `SearchState`.

    No HTTP involved, so tests can call `kb.service` functions directly (for
    example `ingest_url(..., transport=...)`). The settings object is built here
    with an explicit near-duplicate cutoff instead of relying on the default.
    """

    conn: sqlite3.Connection
    state: SearchState
    embedder: FakeEmbedder
    reranker: FakeReranker
    path: Path


@pytest.fixture
def svc(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Svc]:
    """Connection plus a `SearchState` with fake models, loaded and indexed."""
    monkeypatch.delenv("KB_BUCKET", raising=False)
    settings = Settings(db_path=str(db_path), dup_threshold=0.8)
    conn = db.connect(db_path)
    db.init_schema(conn)
    embedder, reranker = FakeEmbedder(), FakeReranker()
    state = indexing.build_state(settings, embedder, reranker)
    asyncio.run(indexing.start(state))
    yield Svc(conn, state, embedder, reranker, db_path)
    conn.close()
