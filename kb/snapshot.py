"""Continuous snapshots of the SQLite database to S3.

The database lives on the instance's root volume. To survive losing the
instance, the app uploads a consistent snapshot to
`s3://<bucket>/snapshots/kb-latest.db.gz` shortly after every committed write:

* `kb.db` reports each committed write to its commit listeners (see the module
  docstring there). `Snapshotter` registers one listener, which only sets a
  flag, so a write never waits for S3.
* One background thread sleeps until the flag is set, waits `interval` seconds
  so a burst of writes collapses into one upload, then calls
  `kb.backup.create_snapshot` (SQLite backup API to a temp file, integrity
  check, gzip, upload). Bucket versioning keeps the previous snapshots, so the
  history is in S3.
* A failed upload is logged and retried one interval later; it never reaches a
  request.
* `stop()` (FastAPI lifespan exit, which runs on SIGTERM from `docker stop`)
  flushes a last snapshot when the flag is still set.

Snapshots are off when `KB_BUCKET` is unset or `KB_SNAPSHOTS=off`
(`Settings.snapshots_enabled`); `from_settings` then returns `None`.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kb import db
from kb.backup import SNAPSHOT_KEY, BackupError, create_snapshot
from kb.config import Settings

log = logging.getLogger("kb.snapshot")

# How long `stop()` waits for an upload that is already running before it
# flushes (the container's stop timeout is 30 seconds, set in the user data).
JOIN_TIMEOUT = 20.0


@dataclass(frozen=True)
class SnapshotResult:
    """What one snapshot upload produced."""

    key: str
    bytes_uploaded: int
    docs: int
    ms: int


class Snapshotter:
    """Uploads the database to S3 at most once per `interval` seconds while writes keep coming.

    `start()` registers the commit listener and starts the thread; `stop()`
    unregisters it, ends the thread and flushes. `snapshot_now()` runs one
    upload in the calling thread (used by the thread and by `stop`).
    """

    def __init__(
        self,
        db_path: str | Path,
        bucket: str,
        *,
        interval: float = 5.0,
        region: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.bucket = bucket
        self.interval = interval
        self._region = region
        self._client = client
        self._key = db.db_key(db_path)
        self._lock = threading.Lock()  # guards _dirty
        self._upload_lock = threading.Lock()  # one upload at a time
        self._dirty = False
        self._wake = threading.Event()  # set when dirty or stopping
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self.last: SnapshotResult | None = None
        self.failures = 0

    # --- dirty flag --------------------------------------------------------------------

    @property
    def dirty(self) -> bool:
        """True when a committed write has not been uploaded yet."""
        with self._lock:
            return self._dirty

    def mark_dirty(self) -> None:
        """Record that the database changed; the thread uploads it after the debounce."""
        with self._lock:
            self._dirty = True
        self._wake.set()

    def _on_commit(self, key: str) -> None:
        if key == self._key:
            self.mark_dirty()

    # --- lifecycle ---------------------------------------------------------------------

    def start(self) -> None:
        """Register the commit listener and start the upload thread (idempotent)."""
        if self._thread is not None:
            return
        db.add_commit_listener(self._on_commit)
        self._thread = threading.Thread(target=self._run, name="kb-snapshots", daemon=True)
        self._thread.start()
        log.info(
            "snapshots on",
            extra={"bucket": self.bucket, "key": SNAPSHOT_KEY, "interval_s": self.interval},
        )

    def stop(self, *, flush: bool = True) -> None:
        """Stop the thread, unregister the listener and, with `flush`, upload what is pending.

        Safe to call twice. Never raises: a failed final upload is logged.
        """
        db.remove_commit_listener(self._on_commit)
        self._stopping.set()
        self._wake.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=JOIN_TIMEOUT)
        if flush:
            self._flush("shutdown")

    # --- upload ------------------------------------------------------------------------

    def _run(self) -> None:
        while True:
            self._wake.wait()
            if self._stopping.is_set():
                return
            # Debounce: let a burst of writes finish, then upload once. The wait
            # ends early only on shutdown, where stop() does the final flush.
            if self._stopping.wait(self.interval):
                return
            self._flush("interval")

    def _flush(self, reason: str) -> None:
        """Upload a snapshot if the database is dirty. Failures are logged, then retried."""
        with self._upload_lock:
            with self._lock:
                if not self._dirty:
                    self._wake.clear()
                    return
                # Cleared before the copy starts: a write that commits during the
                # upload sets it again and is covered by the next snapshot.
                self._dirty = False
                self._wake.clear()
            try:
                result = self.snapshot_now()
            except BackupError as exc:
                self._failed(reason, str(exc))
            except Exception as exc:  # the thread must survive anything
                log.exception("snapshot crashed", extra={"reason": reason})
                self._failed(reason, f"{type(exc).__name__}: {exc}")
            else:
                if result is not None:
                    self.last = result

    def _failed(self, reason: str, error: str) -> None:
        self.failures += 1
        log.error(
            "snapshot failed",
            extra={"reason": reason, "error": error, "retry": reason == "interval"},
        )
        with self._lock:
            self._dirty = True
        if reason == "interval":
            self._wake.set()  # the thread retries after one more interval

    def snapshot_now(self) -> SnapshotResult | None:
        """Upload a snapshot now, in the calling thread. Returns None when there is no database file.

        Raises `BackupError` when the copy, the integrity check or the upload fails.
        """
        if not self.db_path.is_file():
            log.warning("snapshot skipped: no database file", extra={"db_path": str(self.db_path)})
            return None
        started = time.monotonic()
        result = create_snapshot(self.db_path, self.bucket, client=self._client, region=self._region)
        ms = int((time.monotonic() - started) * 1000)
        log.info(
            "snapshot uploaded",
            extra={"key": result.key, "bytes": result.bytes_uploaded, "ms": ms, "docs": result.docs},
        )
        return SnapshotResult(key=result.key, bytes_uploaded=result.bytes_uploaded, docs=result.docs, ms=ms)


def from_settings(settings: Settings, *, client: Any | None = None) -> Snapshotter | None:
    """Return a `Snapshotter` for `settings`, or None when snapshots are off.

    Snapshots copy the SQLite file, so they are always off in PostgreSQL mode
    (the S3 export, `kb.portable`, covers that case).
    """
    if settings.database_url is not None:
        log.info("snapshots off", extra={"reason": "PostgreSQL mode"})
        return None
    if settings.bucket is None or not settings.snapshots:
        reason = "KB_BUCKET is not set" if settings.bucket is None else "KB_SNAPSHOTS=off"
        log.info("snapshots off", extra={"reason": reason})
        return None
    return Snapshotter(
        settings.db_path,
        settings.bucket,
        interval=settings.snapshot_interval,
        region=settings.aws_region,
        client=client,
    )
