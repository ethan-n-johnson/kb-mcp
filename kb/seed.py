"""Seed the knowledge base from a YAML manifest (`python -m kb.cli seed`, phase 11).

The manifest (`seeds/seeds.yaml`) lists sections of entries. Each entry has
exactly one of `url` (fetched with the SSRF-guarded fetcher) or `path` (a file
relative to the manifest), and optional `tags`, `title` and `format`. A section
may set `tags` (added to every entry's) and `base_url` (joined with relative
entry URLs). The optional `planted` block names entries whose `verified_at`
and `updated_at` are backdated so `stale` has something to show.

Every document goes through `service.upload`, the same path as the REST and
MCP uploads, with the actor `cli` and `force=True`: many seeded pages come from
one docs site and must not stop each other at the near-duplicate check (the
eval command measures those similarities instead).

Each URL entry is stored with the manifest URL as `source_url` (`ingest_url`
stores the final URL after redirects instead), so reruns and the eval questions
can find it by the URL written in the manifest.

The run is idempotent: an entry whose URL is already stored is skipped without
fetching, and one whose content hash matches is reported as skipped by the
upload itself. One failing entry never stops the run; it is reported with its
reason.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

import yaml
from pydantic import ValidationError

from kb import db, service
from kb.errors import ServiceError
from kb.ingest.convert import detect_format
from kb.ingest.errors import IngestError
from kb.ingest.fetch import fetch_url
from kb.schemas import UploadRequest
from kb.search.state import SearchState
from kb.store import Store

ACTOR = "cli"
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class ManifestError(ValueError):
    """The manifest file is missing, not YAML, or has an invalid entry."""


@dataclass(frozen=True)
class Entry:
    """One document to seed: a URL or a local file."""

    url: str | None = None
    path: Path | None = None
    tags: tuple[str, ...] = ()
    title: str | None = None
    format: str | None = None

    @property
    def label(self) -> str:
        return self.url if self.url is not None else str(self.path)


@dataclass(frozen=True)
class Manifest:
    """A parsed manifest: entries in order, the planted stale URLs and the fetch delay."""

    entries: list[Entry]
    stale: list[str] = field(default_factory=list)
    stale_days: int = 200
    delay_seconds: float = 0.0


@dataclass(frozen=True)
class Outcome:
    """What happened to one entry (or one planted step)."""

    label: str
    status: str  # added, skipped, failed, ok (dry run), stale
    detail: str = ""
    doc_id: int | None = None


def _tags(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(t, str) and t.strip() for t in value):
        raise ManifestError(f"{where}: tags must be a list of non-empty strings")
    return tuple(t.strip() for t in value)


def _check_http(url: str, where: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ManifestError(f"{where}: {url!r} is not an http(s) URL")
    return url


def load_manifest(path: str | Path) -> Manifest:
    """Read and validate a manifest. Raises `ManifestError` with the offending place."""
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestError(f"cannot read {path}: {exc.strerror}") from exc
    except yaml.YAMLError as exc:
        raise ManifestError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("sections"), list):
        raise ManifestError(f"{path}: expected a mapping with a 'sections' list")
    base_dir = path.parent
    entries: list[Entry] = []
    seen: set[str] = set()
    for s_index, section in enumerate(raw["sections"], start=1):
        where = f"section {s_index}"
        if not isinstance(section, dict) or not isinstance(section.get("entries"), list):
            raise ManifestError(f"{where}: expected a mapping with an 'entries' list")
        section_tags = _tags(section.get("tags"), where)
        base_url = section.get("base_url")
        if base_url is not None:
            base_url = _check_http(str(base_url), where)
        for e_index, item in enumerate(section["entries"], start=1):
            ewhere = f"{where}, entry {e_index}"
            if not isinstance(item, dict):
                raise ManifestError(f"{ewhere}: expected a mapping")
            has_url, has_path = item.get("url") is not None, item.get("path") is not None
            if has_url == has_path:
                raise ManifestError(f"{ewhere}: give exactly one of 'url' or 'path'")
            tags = tuple(dict.fromkeys([*section_tags, *_tags(item.get("tags"), ewhere)]))
            title = item.get("title")
            fmt = item.get("format")
            if has_url:
                url = str(item["url"]).strip()
                if base_url is not None:
                    url = urljoin(base_url, url)
                entry = Entry(url=_check_http(url, ewhere), tags=tags, title=title, format=fmt)
            else:
                entry = Entry(path=base_dir / str(item["path"]), tags=tags, title=title, format=fmt)
            if entry.label in seen:
                raise ManifestError(f"{ewhere}: {entry.label} is listed twice")
            seen.add(entry.label)
            entries.append(entry)
    planted = raw.get("planted") or {}
    if not isinstance(planted, dict):
        raise ManifestError(f"{path}: 'planted' must be a mapping")
    stale = [str(u).strip() for u in planted.get("stale") or []]
    for url in stale:
        if url not in seen:
            raise ManifestError(f"planted.stale: {url} is not an entry of the manifest")
    try:
        stale_days = int(planted.get("stale_days", 200))
        delay = float(raw.get("delay_seconds", 0.0))
    except (TypeError, ValueError) as exc:
        raise ManifestError(f"{path}: stale_days and delay_seconds must be numbers") from exc
    if stale_days < 1 or delay < 0:
        raise ManifestError(f"{path}: stale_days must be at least 1 and delay_seconds not negative")
    return Manifest(entries=entries, stale=stale, stale_days=stale_days, delay_seconds=delay)


def _doc_by_url(conn: Store, url: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM docs WHERE source_url = ? ORDER BY id LIMIT 1", (url,)
    ).fetchone()
    return None if row is None else int(row["id"])


def _prepare(entry: Entry, transport: Any) -> tuple[UploadRequest, bytes, str | None]:
    """Read or fetch the entry: the upload request, the raw bytes and a filename."""
    if entry.url is not None:
        fetched = fetch_url(entry.url, transport=transport)
        fmt = entry.format or detect_format(content_type=fetched.content_type, url=fetched.final_url)
        # The manifest URL, not the final URL after redirects: the rerun check and
        # the eval questions look documents up by the URL written in the manifest.
        source_url = entry.url
        data, filename = fetched.body, None
    else:
        assert entry.path is not None
        try:
            data = entry.path.read_bytes()
        except OSError as exc:
            raise ServiceError("bad_path", f"cannot read {entry.path}: {exc.strerror}", 400) from exc
        fmt = entry.format or detect_format(filename=entry.path.name)
        source_url, filename = None, entry.path.name
    try:
        req = UploadRequest(
            content="",
            format=fmt,
            title=entry.title,
            tags=list(entry.tags),
            source_url=source_url,
            force=True,
        )
    except ValidationError as exc:
        raise ServiceError("bad_request", f"invalid entry: {exc.errors()[0]['msg']}", 400) from exc
    return req, data, filename


def _seed_entry(
    conn: Store,
    entry: Entry,
    search_state: SearchState | None,
    dry_run: bool,
    transport: Any,
) -> Outcome:
    if entry.url is not None and not dry_run:
        existing = _doc_by_url(conn, entry.url)
        if existing is not None:
            return Outcome(entry.label, "skipped", "URL already stored", existing)
    req, data, filename = _prepare(entry, transport)
    if dry_run:
        prepared = service.prepare_upload(req, filename=filename, file_bytes=data)
        words = sum(c.word_count for c in prepared.chunks)
        return Outcome(entry.label, "ok", f"{len(prepared.chunks)} chunks, {words} words: {prepared.title}")
    result = service.upload(
        conn, req, ACTOR, filename=filename, file_bytes=data, search_state=search_state
    )
    if result.status == "created":
        return Outcome(entry.label, "added", f"{result.chunks} chunks", result.doc_id)
    if result.status == "duplicate":
        return Outcome(entry.label, "skipped", result.message or "identical content", result.doc_id)
    return Outcome(entry.label, "failed", result.message or result.status)


def _apply_stale(
    conn: Store, manifest: Manifest, doc_ids: dict[str, int], now: datetime
) -> list[Outcome]:
    """Backdate `verified_at` and `updated_at` of the planted stale docs.

    Docs already older than the target keep their dates, so a rerun changes
    nothing. Each backdated doc gets a `verify` audit row with a note.
    """
    then = (now - timedelta(days=manifest.stale_days)).strftime(_TIME_FORMAT)
    cutoff = (now - timedelta(days=manifest.stale_days - 1)).strftime(_TIME_FORMAT)
    outcomes: list[Outcome] = []
    for url in manifest.stale:
        doc_id = doc_ids.get(url) or _doc_by_url(conn, url)
        if doc_id is None:
            outcomes.append(Outcome(url, "failed", "planted stale: the doc was not seeded"))
            continue
        row = conn.execute(
            "SELECT updated_at, verified_at FROM docs WHERE id = ?", (doc_id,)
        ).fetchone()
        if row["updated_at"] <= cutoff and (row["verified_at"] or "") <= cutoff:
            outcomes.append(Outcome(url, "skipped", "planted stale: already backdated", doc_id))
            continue
        with db.transaction(conn):
            conn.execute(
                "UPDATE docs SET updated_at = ?, verified_at = ?, verified_by = ? WHERE id = ?",
                (then, then, ACTOR, doc_id),
            )
            conn.execute(
                "INSERT INTO audit_log (token_name, action, doc_id, at, note) VALUES (?, ?, ?, ?, ?)",
                (ACTOR, "verify", doc_id, db.utcnow(), f"seed: backdated {manifest.stale_days} days"),
            )
        outcomes.append(Outcome(url, "stale", f"backdated {manifest.stale_days} days", doc_id))
    return outcomes


def seed(
    conn: Store,
    manifest: Manifest,
    *,
    search_state: SearchState | None = None,
    dry_run: bool = False,
    transport: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    now: datetime | None = None,
    progress: Callable[[Outcome], None] | None = None,
) -> list[Outcome]:
    """Seed every manifest entry, then apply the planted stale dates.

    `dry_run` fetches and converts each entry but writes nothing (and skips the
    planted step). `transport` (an `httpx.MockTransport`) and `sleep` are for
    tests. `progress` is called with each outcome as it happens.
    """
    outcomes: list[Outcome] = []
    doc_ids: dict[str, int] = {}
    fetched_before = False
    for entry in manifest.entries:
        will_fetch = entry.url is not None and (dry_run or _doc_by_url(conn, entry.url) is None)
        if will_fetch and fetched_before and manifest.delay_seconds:
            sleep(manifest.delay_seconds)  # be polite to the docs sites
        fetched_before = fetched_before or will_fetch
        try:
            outcome = _seed_entry(conn, entry, search_state, dry_run, transport)
        except (IngestError, ServiceError) as exc:
            outcome = Outcome(entry.label, "failed", f"{exc.code}: {exc.message}")
        except Exception as exc:  # one broken entry must never stop the run
            outcome = Outcome(entry.label, "failed", f"{type(exc).__name__}: {exc}")
        if outcome.doc_id is not None:
            doc_ids[entry.label] = outcome.doc_id
        outcomes.append(outcome)
        if progress is not None:
            progress(outcome)
    if not dry_run:
        for outcome in _apply_stale(conn, manifest, doc_ids, now or datetime.now(timezone.utc)):
            outcomes.append(outcome)
            if progress is not None:
                progress(outcome)
    return outcomes


def summary(outcomes: list[Outcome]) -> dict[str, int]:
    """Count outcomes by status."""
    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome.status] = counts.get(outcome.status, 0) + 1
    return counts
