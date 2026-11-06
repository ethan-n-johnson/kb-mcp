"""Health monitor for PostgreSQL mode: `/healthz` answers from a cache a thread keeps fresh.

The load balancer kills an instance whose `/healthz` misses three checks in a
row. A check that needs a pooled connection, or a worker thread, waits behind
the searches that hold them, so under load it misses its deadline although the
instance is fine. The monitor takes both dependencies away from the request:

- A plain daemon thread (not the anyio pool) owns one database connection of
  its own (not from the shared pool), reused while it works and reopened after
  an error. Every `interval` seconds it counts the chunks and the embedded
  chunks, and stores `(chunks, indexed, checked_at)` under a lock.
- `/healthz` only reads that snapshot (`snapshot()`): the counts while the last
  success is younger than `max_age` (60 s), else None, which is a 503.
- The same thread reads the keyword-fallback toggle every
  `SearchState.TOGGLE_CACHE_SECONDS` and hands it to the `SearchState`, so no
  request path reads the database for it (`SearchState.monitored`).

The database URL carries the password: it is passed to psycopg and never logged.
SQLite mode needs no monitor: its shared connection answers on the event loop.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

import psycopg

from kb.search.state import SearchState

log = logging.getLogger(__name__)

CHECK_INTERVAL = 10.0
MAX_AGE = 60.0
CONNECT_TIMEOUT = 3
STATEMENT_TIMEOUT_MS = 5000


class HealthMonitor:
    """Keeps a fresh `(chunks, indexed)` snapshot and the fallback toggle for `state`."""

    def __init__(
        self,
        url: str,
        state: SearchState,
        *,
        interval: float = CHECK_INTERVAL,
        max_age: float = MAX_AGE,
        toggle_interval: float = SearchState.TOGGLE_CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = url
        self._state = state
        self.interval = interval
        self.max_age = max_age
        self.toggle_interval = toggle_interval
        self._clock = clock
        self._lock = threading.Lock()  # guards the snapshot
        self._conn_lock = threading.Lock()  # serializes use of the connection
        self._conn: psycopg.Connection | None = None
        self._snapshot: tuple[int, int, float] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failed_logged = False

    # --- connection ---------------------------------------------------------------

    def _connection(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(
                self._url,
                autocommit=True,
                connect_timeout=CONNECT_TIMEOUT,
                options=f"-c statement_timeout={STATEMENT_TIMEOUT_MS}",
            )
        return self._conn

    def _drop_connection(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # already broken; nothing to clean up
                pass

    # --- the two jobs -------------------------------------------------------------

    def check_once(self) -> bool:
        """Count the chunks now; True on success. Never raises."""
        try:
            with self._conn_lock:
                row = self._connection().execute(
                    "SELECT count(*), count(embedding) FROM chunks"
                ).fetchone()
            counts = (int(row[0]), int(row[1]), self._clock())
        except Exception as exc:
            with self._conn_lock:
                self._drop_connection()
            if not self._failed_logged:
                log.warning("health check cannot read the database", extra={"error": type(exc).__name__})
                self._failed_logged = True
            return False
        self._failed_logged = False
        with self._lock:
            self._snapshot = counts
        return True

    def refresh_toggle(self) -> bool:
        """Read the `fallback` row now and hand it to the state; True on success. Never raises."""
        try:
            with self._conn_lock:
                row = self._connection().execute(
                    "SELECT value FROM settings WHERE key = %s", ("fallback",)
                ).fetchone()
        except Exception as exc:
            with self._conn_lock:
                self._drop_connection()
            self._state.toggle_read_failed(exc)
            return False
        self._state.apply_toggle(None if row is None else row[0])
        return True

    # --- what /healthz reads ------------------------------------------------------

    def snapshot(self) -> tuple[int, int] | None:
        """`(chunks, indexed)` from the last successful check, or None when it is too old (or absent)."""
        with self._lock:
            snap = self._snapshot
        if snap is None or self._clock() - snap[2] > self.max_age:
            return None
        return snap[0], snap[1]

    # --- thread -------------------------------------------------------------------

    def prime(self) -> None:
        """One synchronous check and toggle read, so the first `/healthz` already has data.

        Blocking: call it in a worker thread, before serving traffic.
        """
        self.check_once()
        self.refresh_toggle()

    def start(self) -> None:
        """Start the daemon thread; from now on the state's toggle comes from it."""
        if self._thread is not None:
            return
        self._state.monitored = True
        self._thread = threading.Thread(target=self._run, name="kb-health-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the thread (short wait) and close the connection. Safe to call twice."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2.0)
        with self._conn_lock:
            self._drop_connection()

    def _run(self) -> None:
        every = max(1, round(self.interval / self.toggle_interval))
        tick = 0
        while not self._stop.wait(self.toggle_interval):
            tick += 1
            self.refresh_toggle()
            if tick % every == 0:
                self.check_once()
