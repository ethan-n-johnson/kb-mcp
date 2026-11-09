"""The PostgreSQL pool's settings, checked without a database (runs on every machine).

The behavior itself (a connection killed by a failover is replaced before a
request uses it) is pinned by the `pg` test
`test_postgres_app.py::test_connections_killed_by_a_failover_are_replaced_before_use`.
"""

from __future__ import annotations

from typing import Any

import pytest
from psycopg_pool import ConnectionPool

from kb.store import postgres


class _Boot:
    def __enter__(self) -> "_Boot":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _RecordingPool:
    created: list[dict[str, Any]] = []
    check_connection = staticmethod(ConnectionPool.check_connection)

    def __init__(self, url: str, **kwargs: Any) -> None:
        _RecordingPool.created.append(kwargs)

    def open(self, wait: bool = False, timeout: float = 0) -> None:
        return None

    def close(self) -> None:
        return None


def test_the_pool_checks_each_connection_before_handing_it_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(postgres.psycopg, "connect", lambda *a, **k: _Boot())
    monkeypatch.setattr(postgres, "apply_schema", lambda conn: None)
    monkeypatch.setattr(postgres, "ConnectionPool", _RecordingPool)
    monkeypatch.setattr(postgres, "_pools", {})
    postgres.get_pool("postgresql://u@h/db")
    kwargs = _RecordingPool.created[-1]
    assert kwargs["check"] is ConnectionPool.check_connection
    assert (kwargs["min_size"], kwargs["max_size"]) == (postgres.POOL_MIN, postgres.POOL_MAX)
