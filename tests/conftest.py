"""Shared fixtures: temp database and a TestClient with models off.

Two storage backends
--------------------
Tests run on SQLite by default. A test module marked `pytestmark = pytest.mark.dual`
runs every test twice: on SQLite and on PostgreSQL (the `postgres` leg is marked
`pg` and skipped unless `KB_TEST_DATABASE_URL` is set). The `backend` fixture picks
the leg: for `postgres` it sets `KB_DATABASE_URL`, so `Settings.from_env()`, the
app (`client`, `open_app`) and the `conn`/`store`/`svc` fixtures all use PostgreSQL,
and it empties every table first. Locally:

    docker run -d --name kb-pg -e POSTGRES_PASSWORD=dev -p 5432:5432 pgvector/pgvector:pg16
    KB_TEST_DATABASE_URL=postgresql://postgres:dev@localhost:5432/postgres pytest -q -m "not models"
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, ContextManager, Iterator

import pytest
from fastapi.testclient import TestClient

from kb import config, ratelimit
from kb.config import Settings
from kb.search import indexing
from kb.search.state import SearchState
from kb.store import Store, close_pools, init_schema, open_store
from tests.fakes import FakeEmbedder, FakeReranker


BACKENDS = ["sqlite", pytest.param("postgres", marks=pytest.mark.pg)]


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Run tests of a `dual`-marked module on both backends (when they use a database fixture)."""
    if metafunc.definition.get_closest_marker("dual") and "backend" in metafunc.fixturenames:
        metafunc.parametrize("backend", BACKENDS, indirect=True)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "dual: run every test on SQLite and on PostgreSQL")


_TABLES = "gaps, audit_log, doc_tags, chunks, docs"


def _empty_postgres(url: str) -> None:
    """Create the schema if needed and empty every table (ids restart at 1)."""
    from kb.store.postgres import get_pool

    with get_pool(url).connection() as c:
        c.execute(f"TRUNCATE {_TABLES} RESTART IDENTITY")  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _no_ambient_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's own `KB_DATABASE_URL` must never leak into a SQLite test."""
    monkeypatch.delenv("KB_DATABASE_URL", raising=False)
    monkeypatch.delenv("KB_DATABASE_URL_SOURCE", raising=False)
    config.clear_database_url_cache()


@pytest.fixture
def backend(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """`sqlite` (default) or `postgres` (the second leg of a `dual` module)."""
    kind = getattr(request, "param", "sqlite")
    if kind == "postgres":
        url = os.environ.get("KB_TEST_DATABASE_URL")
        if not url:
            pytest.skip("KB_TEST_DATABASE_URL is not set")
        monkeypatch.setenv("KB_DATABASE_URL", url)
        _empty_postgres(url)
    yield kind
    if kind == "postgres":
        close_pools()


@pytest.fixture
def pg_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[Store]:
    """An empty PostgreSQL database for tests that exist only for PostgreSQL (`@pytest.mark.pg`)."""
    url = os.environ.get("KB_TEST_DATABASE_URL")
    if not url:
        pytest.skip("KB_TEST_DATABASE_URL is not set")
    monkeypatch.setenv("KB_DATABASE_URL", url)
    _empty_postgres(url)
    store = open_store(Settings.from_env())
    yield store
    store.close()
    close_pools()


def make_settings(db_path: Path, **overrides: object) -> Settings:
    """`Settings` for the current backend: the temp SQLite path, or PostgreSQL from the env."""
    base = Settings.from_env()
    return dataclasses.replace(base, db_path=str(db_path), **overrides)  # type: ignore[arg-type]


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Path of a not-yet-created database file inside a temp directory."""
    return tmp_path / "data" / "kb.db"


@pytest.fixture
def conn(db_path: Path, backend: str) -> Iterator[Store]:
    """A connection to a fresh database (temp SQLite file or empty PostgreSQL) with the schema applied."""
    c = open_store(make_settings(db_path))
    init_schema(c)
    yield c
    c.close()


@pytest.fixture
def store(conn: Store) -> Store:
    """The `Store` for the current backend (the same connection as `conn`)."""
    return conn


@pytest.fixture
def client(
    db_path: Path, backend: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
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
def open_app(db_path: Path, backend: str, monkeypatch: pytest.MonkeyPatch) -> OpenApp:
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

    conn: Store
    state: SearchState
    embedder: FakeEmbedder
    reranker: FakeReranker
    path: Path


@pytest.fixture
def svc(db_path: Path, backend: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Svc]:
    """Connection plus a `SearchState` with fake models, loaded and indexed."""
    monkeypatch.delenv("KB_BUCKET", raising=False)
    settings = dataclasses.replace(
        Settings(db_path=str(db_path), dup_threshold=0.8),
        database_url=os.environ.get("KB_DATABASE_URL"),
    )
    conn = open_store(settings)
    init_schema(conn)
    embedder, reranker = FakeEmbedder(), FakeReranker()
    state = indexing.build_state(settings, embedder, reranker)
    asyncio.run(indexing.start(state))
    yield Svc(conn, state, embedder, reranker, db_path)
    conn.close()
