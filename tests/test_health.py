"""/healthz contract, and that it cannot be starved by load (phase 23a)."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import contextmanager
from typing import Any, Iterator

import pytest
from anyio import to_thread
from fastapi.testclient import TestClient
from starlette.concurrency import run_in_threadpool

from kb.config import Settings
from kb.health import HealthMonitor
from kb.search.state import SearchState
from kb.store import Store
from tests.conftest import AUTH, OpenApp
from tests.helpers import fresh_health


def test_healthz_ok(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "mode": "keyword",
        "models": {"embedder": "off", "reranker": "off"},
        "fallback_toggle": False,
        "chunks": 0,
        "index_size": 0,
    }


def test_healthz_counts_chunks(client: TestClient) -> None:
    conn = client.app.state.conn
    conn.execute(
        "INSERT INTO docs(title, source_type, content_md, content_hash, created_at, updated_at)"
        " VALUES ('T', 'markdown', 'b', 'x', 'n', 'n')"
    )
    conn.execute("INSERT INTO chunks(doc_id, ord, heading_path, text) VALUES (1, 0, 'h', 't')")
    assert client.get("/healthz").json()["chunks"] == 1


def test_healthz_503_when_db_unreadable(client: TestClient) -> None:
    client.app.state.conn.close()
    resp = client.get("/healthz")
    assert resp.status_code == 503
    assert resp.json()["status"] == "error"


# --- /healthz under load (phase 23a) -------------------------------------------------------

class Clock:
    """A settable clock for `HealthMonitor`."""

    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


HEALTH_KEYS = {"status", "mode", "models", "fallback_toggle", "chunks", "index_size"}


@contextmanager
def thread_pool_held(client: TestClient) -> Iterator[None]:
    """Use up every worker thread token of the app's event loop (the pool the searches share)."""

    async def take() -> tuple[Any, object, float]:
        limiter = to_thread.current_default_thread_limiter()
        borrower, old = object(), limiter.total_tokens
        limiter.total_tokens = 1
        await limiter.acquire_on_behalf_of(borrower)
        return limiter, borrower, old

    async def starved() -> bool:
        try:
            await asyncio.wait_for(run_in_threadpool(lambda: None), 0.2)
        except asyncio.TimeoutError:
            return True
        return False

    limiter, borrower, old = client.portal.call(take)
    try:
        assert client.portal.call(starved), "the thread pool is not exhausted"
        yield
    finally:
        limiter.release_on_behalf_of(borrower)
        limiter.total_tokens = old


def _timed_health(client: TestClient) -> tuple[Any, float]:
    started = time.perf_counter()
    resp = client.get("/healthz")
    return resp, time.perf_counter() - started


def test_healthz_json_keys_are_unchanged(client: TestClient) -> None:
    body = client.get("/healthz").json()
    assert set(body) == HEALTH_KEYS
    assert set(body["models"]) == {"embedder", "reranker"}


def test_healthz_answers_in_under_200_ms_while_every_worker_thread_is_busy(client: TestClient) -> None:
    with thread_pool_held(client):
        resp, elapsed = _timed_health(client)
    assert resp.status_code == 200 and set(resp.json()) == HEALTH_KEYS
    assert elapsed < 0.2


@pytest.fixture
def monitor_clock() -> Clock:
    return Clock(time.monotonic())


@pytest.mark.pg
def test_healthz_does_not_wait_for_the_pool_or_the_toggle_read(
    open_app: OpenApp, pg_store: Store
) -> None:
    from kb.store.postgres import POOL_MAX, get_pool

    with open_app(models=False) as client:
        state = client.app.state.search
        pool = get_pool(client.app.state.settings.database_url)
        # The `pg_store` fixture holds one pooled connection; with these the whole pool is checked out.
        held = [pool.getconn() for _ in range(POOL_MAX - 1)]
        try:
            state.clock = lambda: time.monotonic() + 3600  # a toggle cache that is long stale
            state._toggle_read_at = 0.0
            with thread_pool_held(client):
                resp, elapsed = _timed_health(client)
        finally:
            for conn in held:
                pool.putconn(conn)
    assert resp.status_code == 200 and set(resp.json()) == HEALTH_KEYS
    assert elapsed < 0.2


@pytest.mark.pg
def test_healthz_is_503_when_the_last_check_is_older_than_60_seconds_and_200_after_a_new_one(
    open_app: OpenApp, pg_store: Store, monitor_clock: Clock
) -> None:
    with open_app(models=False) as client:
        monitor = client.app.state.health
        monitor.stop()
        monitor._clock = monitor_clock
        assert monitor.check_once()
        monitor_clock.now += 59
        assert client.get("/healthz").status_code == 200
        monitor_clock.now += 2
        stale = client.get("/healthz")
        assert stale.status_code == 503 and stale.json() == {"status": "error"}
        assert monitor.check_once()
        again = client.get("/healthz")
    assert again.status_code == 200 and set(again.json()) == HEALTH_KEYS


@pytest.mark.pg
def test_healthz_counts_come_from_the_monitor_s_last_check(
    open_app: OpenApp, pg_store: Store
) -> None:
    with open_app(models=False) as client:
        client.app.state.health.stop()
        assert client.get("/healthz").json()["chunks"] == 0
        pg_store.execute(
            "INSERT INTO docs(title, source_type, content_md, content_hash, created_at, updated_at)"
            " VALUES ('T', 'markdown', 'b', 'x', now(), now())"
        )
        pg_store.execute("INSERT INTO chunks(doc_id, ord, heading_path, text) VALUES (1, 0, 'h', 't')")
        assert client.get("/healthz").json()["chunks"] == 0  # cached, not counted per request
        assert fresh_health(client)["chunks"] == 1


@pytest.mark.pg
def test_the_monitor_hands_the_toggle_to_the_state_and_search_never_reads_it_itself(
    open_app: OpenApp, pg_store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    with open_app(models=False) as a, open_app(models=False) as b:
        monitor, state = b.app.state.health, b.app.state.search
        monitor.stop()
        assert state.monitored
        a.post("/api/admin/fallback", json={"on": True}, headers=AUTH)

        def forbidden(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("the request path read the database for the toggle")

        monkeypatch.setattr("kb.store.open_store", forbidden)
        state.clock = lambda: time.monotonic() + 3600
        state._toggle_read_at = 0.0
        assert state.fallback_toggle is False  # nothing has fetched the new value yet
        assert monitor.refresh_toggle()
        assert state.fallback_toggle is True


@pytest.mark.pg
def test_a_monitor_that_cannot_reach_the_database_keeps_the_toggle_and_reports_stale(
    pg_store: Store, caplog: pytest.LogCaptureFixture, monitor_clock: Clock
) -> None:
    url = "postgresql://user:hunter2pw@127.0.0.1:1/none"
    state = SearchState(Settings.from_env())
    state.apply_toggle("on")
    monitor = HealthMonitor(url, state, clock=monitor_clock)
    with caplog.at_level(logging.WARNING):
        assert not monitor.check_once() and not monitor.check_once()
        assert not monitor.refresh_toggle() and not monitor.refresh_toggle()
    assert monitor.snapshot() is None
    assert state._toggle is True
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert sum("health check cannot read" in w for w in warnings) == 1
    assert sum("cannot read the fallback toggle" in w for w in warnings) == 1
    assert "hunter2pw" not in " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)


@pytest.mark.pg
def test_the_monitor_thread_refreshes_both_on_its_own(pg_store: Store) -> None:
    state = SearchState(Settings.from_env())
    monitor = HealthMonitor(state.settings.database_url or "", state, interval=0.2, toggle_interval=0.1)
    monitor.start()
    try:
        assert state.monitored
        pg_store.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES ('fallback', 'on', now())"
            " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not (state.fallback_toggle and monitor.snapshot()):
            time.sleep(0.05)
        assert state.fallback_toggle is True
        assert monitor.snapshot() == (0, 0)
    finally:
        monitor.stop()
    monitor.stop()  # twice is fine
