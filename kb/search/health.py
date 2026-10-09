"""Shared health bookkeeping for the embedder and the reranker."""

from __future__ import annotations

import threading
import time
from typing import Callable

MAX_CONSECUTIVE_FAILURES = 3
PROBE_INTERVAL_SECONDS = 30.0


class HealthTracker:
    """Tracks whether a model loaded and how many calls failed in a row.

    A model is healthy when it loaded and has failed fewer than three calls in
    a row. After the third consecutive failure it is unhealthy until a call
    succeeds. Because an unhealthy model is not used by the pipeline, nothing
    would ever call it again, so once `probe_interval` seconds have passed the
    tracker reports healthy again for one probe: a success clears the failure
    count, another failure re-opens the window.
    """

    def __init__(
        self,
        *,
        probe_interval: float = PROBE_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self._probe_interval = probe_interval
        self._loaded = False
        self._load_failed = False
        self._failures = 0
        self._tripped_at = 0.0

    def mark_loaded(self) -> None:
        """Record a successful load."""
        with self._lock:
            self._loaded, self._load_failed = True, False

    def mark_load_failed(self) -> None:
        """Record a failed load; the model stays unhealthy for the process lifetime."""
        with self._lock:
            self._loaded, self._load_failed = False, True

    def record_success(self) -> None:
        """A call succeeded: clear the consecutive failure count."""
        with self._lock:
            self._failures = 0

    def record_failure(self) -> int:
        """A call failed; returns the consecutive failure count."""
        with self._lock:
            self._failures += 1
            if self._failures >= MAX_CONSECUTIVE_FAILURES:
                self._tripped_at = self._clock()
            return self._failures

    @property
    def consecutive_failures(self) -> int:
        """Failures in a row since the last success."""
        return self._failures

    @property
    def healthy(self) -> bool:
        """True when the model loaded and is not tripped (or a probe is due)."""
        with self._lock:
            if not self._loaded:
                return False
            if self._failures < MAX_CONSECUTIVE_FAILURES:
                return True
            return self._clock() - self._tripped_at >= self._probe_interval

    @property
    def status(self) -> str:
        """`ok`, `failed` (load failed or tripped) or `off` (never loaded)."""
        if self.healthy:
            return "ok"
        with self._lock:
            return "failed" if (self._load_failed or self._loaded) else "off"
