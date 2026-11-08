"""Background gap resolution: one thread per process, one doc at a time.

Gap resolution reranks every open question against every chunk of the written
doc, which on a t3.medium can take minutes for a large PDF. Inside the request
that pushed uploads past the 60 second CloudFront and ALB timeouts (504 while
the doc was in fact stored) and kept both vCPUs busy long enough for the ALB
health check to fail. The server therefore hands the doc id to this worker and
answers at once; the worker resolves the gaps afterwards on its own connection.

Gap bookkeeping is best effort (`kb.service._resolve_gaps_quietly`), so jobs
still queued when the process stops are dropped; the next write of a doc, or
a later doc that answers the same question, resolves them.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import TYPE_CHECKING

from kb.search import resolve

if TYPE_CHECKING:
    from kb.config import Settings
    from kb.search.state import SearchState

log = logging.getLogger(__name__)

_STOP = object()


class GapWorker:
    """Runs `resolve.resolve_gaps` for submitted doc ids on a daemon thread."""

    def __init__(self, settings: "Settings", state: "SearchState") -> None:
        self._settings = settings
        self._state = state
        self._queue: queue.Queue[object] = queue.Queue()
        self._pending: set[int] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="gap-resolution", daemon=True)
            self._thread.start()

    def submit(self, doc_id: int) -> None:
        """Queue `doc_id`; a doc already waiting is not queued twice."""
        with self._lock:
            if doc_id in self._pending:
                return
            self._pending.add(doc_id)
        self._queue.put(doc_id)

    def join(self) -> None:
        """Block until every submitted doc has been handled (tests)."""
        self._queue.join()

    def stop(self, timeout: float = 5.0) -> None:
        """Finish the doc in progress (up to `timeout`), drop the rest."""
        if self._thread is None:
            return
        with self._lock:
            dropped = len(self._pending)
        self._queue.put(_STOP)
        self._thread.join(timeout)
        self._thread = None
        if dropped:
            log.info("gap resolution jobs dropped at shutdown", extra={"dropped": dropped})

    def _run(self) -> None:
        from kb.store import open_store

        while True:
            item = self._queue.get()
            try:
                if item is _STOP:
                    return
                doc_id = int(item)  # type: ignore[call-overload]
                # Taken off the pending set before the work starts, so a write to the
                # same doc while it runs queues it again (its chunks may have changed).
                with self._lock:
                    self._pending.discard(doc_id)
                try:
                    store = open_store(self._settings)
                    try:
                        resolve.resolve_gaps(store, self._state, doc_id)
                    finally:
                        store.close()
                except Exception:
                    log.error("gap resolution failed", exc_info=True, extra={"doc_id": doc_id})
            finally:
                self._queue.task_done()
