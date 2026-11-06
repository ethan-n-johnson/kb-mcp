"""Two instances on one PostgreSQL database (phase 22; `pg` tests).

With 2 to 4 instances behind the load balancer, any instance must be able to
finish what another started. Two apps opened on the same database stand in for
two instances: a one-time upload link issued by one works once on the other, and
the keyword-fallback toggle set on one reaches the other within the cache window.
The in-memory behavior in SQLite mode is covered in `test_upload_links.py`.
"""

from __future__ import annotations

import threading
from typing import Any
from urllib.parse import urlsplit

import pytest

from kb import uploads
from kb.config import Settings
from kb.search.state import SearchState
from kb.store import Store, open_store
from tests.conftest import AUTH, OpenApp
from tests.corpus import EBS

pytestmark = pytest.mark.pg


@pytest.fixture(autouse=True)
def _postgres(pg_store):  # noqa: ANN001 - fixture that selects the PostgreSQL leg for open_app
    yield pg_store


def _path(url: str) -> str:
    return urlsplit(url).path


def _link(client: Any, filename: str = "notes.md") -> dict[str, Any]:
    made = client.post("/api/upload-links", json={"filename": filename, "tags": ["ops"]}, headers=AUTH)
    assert made.status_code == 201, made.text
    return made.json()


def _ticket_of(link: dict[str, Any]) -> str:
    return _path(link["url"]).rsplit("/", 1)[1]


def _rows(store: Store) -> list[Any]:
    return store.fetchall("SELECT * FROM upload_tickets ORDER BY token_hash")


# --- upload tickets ------------------------------------------------------------------------


def test_a_link_issued_on_one_instance_is_accepted_once_on_the_other(open_app: OpenApp) -> None:
    with open_app(models=False) as a, open_app(models=False) as b:
        link = _link(a)
        on_b = b.post(_path(link["url"]), content=EBS.encode())
        again_on_a = a.post(_path(link["url"]), content=EBS.encode())
        again_on_b = b.post(_path(link["url"]), content=EBS.encode())
    assert on_b.status_code == 201 and on_b.json()["status"] == "created"
    assert again_on_a.status_code == 404 and again_on_a.json()["error"] == "bad_ticket"
    assert again_on_b.status_code == 404


def test_the_link_keeps_its_owner_title_and_tags_across_instances(open_app: OpenApp) -> None:
    with open_app(models=False) as a, open_app(models=False) as b:
        made = a.post(
            "/api/upload-links",
            json={"filename": "notes.md", "title": "Runbook", "tags": ["ops", "aws"]},
            headers=AUTH,
        )
        stored = b.post(_path(made.json()["url"]), content=EBS.encode())
        doc = a.get(f"/api/docs/{stored.json()['doc_id']}").json()
    assert stored.status_code == 201
    assert doc["title"] == "Runbook" and sorted(doc["tags"]) == ["aws", "ops"]


def test_the_database_holds_a_hash_and_never_the_ticket(open_app: OpenApp, pg_store: Store) -> None:
    with open_app(models=False) as a:
        link = _link(a)
    ticket = _ticket_of(link)
    rows = _rows(pg_store)
    assert len(rows) == 1
    row = rows[0]
    assert len(row["token_hash"]) == 64 and set(row["token_hash"]) <= set("0123456789abcdef")
    assert row["token_hash"] != ticket
    assert not any(ticket in str(value) for value in tuple(row)), "the ticket text is stored in a column"
    assert row["actor"] == "test" and row["filename"] == "notes.md" and row["used_at"] is None


def test_a_used_ticket_is_marked_used_not_deleted(open_app: OpenApp, pg_store: Store) -> None:
    with open_app(models=False) as a:
        link = _link(a)
        a.post(_path(link["url"]), content=EBS.encode())
    rows = _rows(pg_store)
    assert len(rows) == 1 and rows[0]["used_at"] is not None


def test_two_threads_cannot_both_take_one_ticket(pg_store: Store) -> None:
    settings = Settings.from_env()
    ticket, _ = uploads.DbTicketStore(pg_store).issue("alice", "a.md", title="T", tags=["x"], force=True)
    stores = [open_store(settings) for _ in range(4)]  # the pool holds 5, `pg_store` has one
    barrier = threading.Barrier(len(stores))
    results: list[Any] = []

    def run(store: Store) -> None:
        barrier.wait()
        results.append(uploads.DbTicketStore(store).take(ticket))

    threads = [threading.Thread(target=run, args=(s,)) for s in stores]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for s in stores:
        s.close()
    winners = [r for r in results if r is not None]
    assert len(results) == len(stores) and len(winners) == 1
    won = winners[0]
    assert (won.actor, won.filename, won.title, won.tags, won.force) == ("alice", "a.md", "T", ("x",), True)


def test_an_expired_ticket_is_refused(pg_store: Store) -> None:
    store = uploads.DbTicketStore(pg_store)
    ticket, ttl = store.issue("alice", "a.md")
    assert ttl == uploads.TICKET_TTL_SECONDS and store.pending() == 1
    pg_store.execute("UPDATE upload_tickets SET expires_at = now() - interval '1 second'")
    assert store.pending() == 0
    assert store.take(ticket) is None


def test_the_ticket_lives_for_the_ttl_by_the_database_clock(pg_store: Store) -> None:
    store = uploads.DbTicketStore(pg_store, ttl=90)
    store.issue("alice", "a.md")
    seconds = pg_store.fetchone("SELECT extract(epoch FROM expires_at - now()) AS s FROM upload_tickets")["s"]
    assert 85 < float(seconds) <= 90


def test_unknown_tickets_are_refused(pg_store: Store) -> None:
    assert uploads.DbTicketStore(pg_store).take("made-up") is None


def test_issuing_prunes_expired_rows(pg_store: Store) -> None:
    store = uploads.DbTicketStore(pg_store)
    store.issue("alice", "old.md")
    pg_store.execute("UPDATE upload_tickets SET expires_at = now() - interval '1 second'")
    store.issue("alice", "new.md")
    assert [r["filename"] for r in _rows(pg_store)] == ["new.md"]


def test_pending_tickets_are_capped_oldest_first(pg_store: Store) -> None:
    store = uploads.DbTicketStore(pg_store, max_pending=3)
    first = store.issue("a", "1.md")[0]
    pg_store.execute("UPDATE upload_tickets SET expires_at = expires_at - interval '10 seconds'")
    others = [store.issue("a", f"{i}.md")[0] for i in range(3)]
    assert store.pending() == 3
    assert store.take(first) is None
    assert all(store.take(t) is not None for t in others)


def test_fifty_pending_tickets_are_kept_by_default(pg_store: Store) -> None:
    store = uploads.DbTicketStore(pg_store)
    for i in range(uploads.MAX_PENDING + 5):
        store.issue("a", f"{i}.md")
    assert store.pending() == uploads.MAX_PENDING == 50


def test_used_tickets_do_not_count_against_the_cap(pg_store: Store) -> None:
    store = uploads.DbTicketStore(pg_store, max_pending=2)
    first = store.issue("a", "1.md")[0]
    second = store.issue("a", "2.md")[0]
    assert store.take(first) is not None
    third = store.issue("a", "3.md")[0]
    assert store.take(second) is not None and store.take(third) is not None


def test_store_for_picks_the_database_in_postgres_mode(pg_store: Store) -> None:
    assert isinstance(uploads.store_for(pg_store), uploads.DbTicketStore)


def test_the_mcp_tool_and_the_ticket_route_work_across_instances(open_app: OpenApp) -> None:
    from tests.mcp_helpers import call, ok

    uploads.STORE.clear()
    with open_app(models=False) as a, open_app(models=False) as b:
        link = ok(call(a, "upload_file", {"filename": "notes.md"}, token=True))
        stored = b.post(_path(link["url"]), content=EBS.encode())
    assert stored.status_code == 201
    assert uploads.STORE.pending() == 0  # nothing went to the in-memory store


# --- the fallback toggle ---------------------------------------------------------------------


class Clock:
    """A settable monotonic clock for `SearchState.clock`."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _use_clock(client: Any, clock: Clock) -> None:
    """Give an instance a test clock; its next toggle read happens on the first request.

    The instance's health monitor is stopped, so the toggle is read inline on
    that clock, the way a process without a monitor (stdio MCP, the CLI) reads
    it. The monitor's own refresh is covered in `test_health.py`.
    """
    state = client.app.state.search
    client.app.state.health.stop()
    state.monitored = False
    state.clock = clock
    state._toggle_read_at = None  # startup already read the toggle with the real clock


def _mode(client: Any) -> tuple[bool, str]:
    body = client.get("/healthz").json()
    return body["fallback_toggle"], body["mode"]


def test_the_toggle_set_on_one_instance_reaches_the_other_within_five_seconds(open_app: OpenApp) -> None:
    clock = Clock()
    with open_app() as a, open_app() as b:
        _use_clock(b, clock)
        assert _mode(a) == (False, "semantic") and _mode(b) == (False, "semantic")
        resp = a.post("/api/admin/fallback", json={"on": True}, headers=AUTH)
        assert resp.status_code == 200 and resp.json() == {"fallback_toggle": True}
        assert _mode(a) == (True, "keyword")  # the instance that changed it sees it at once
        clock.now += 4.9
        assert _mode(b) == (False, "semantic")  # inside the cache window
        clock.now += 0.2
        assert _mode(b) == (True, "keyword")
        a.post("/api/admin/fallback", json={"on": False}, headers=AUTH)
        assert _mode(b) == (True, "keyword")
        clock.now += 5.1
        assert _mode(b) == (False, "semantic")


def test_the_toggle_is_a_row_in_settings_next_to_the_audit_row(open_app: OpenApp, pg_store: Store) -> None:
    with open_app(models=False) as a:
        a.post("/api/admin/fallback", json={"on": True}, headers=AUTH)
        row = pg_store.fetchone("SELECT value, updated_at FROM settings WHERE key = 'fallback'")
        actions = [r["action"] for r in pg_store.fetchall("SELECT action FROM audit_log")]
        a.post("/api/admin/fallback", json={"on": False}, headers=AUTH)
        off = pg_store.fetchone("SELECT value FROM settings WHERE key = 'fallback'")
    assert row["value"] == "on" and row["updated_at"]
    assert actions == ["fallback_on"] and off["value"] == "off"


def test_a_new_instance_starts_with_the_stored_toggle(open_app: OpenApp) -> None:
    with open_app(models=False) as a:
        a.post("/api/admin/fallback", json={"on": True}, headers=AUTH)
        with open_app(models=False) as b:
            assert _mode(b)[0] is True


def test_a_missing_row_means_off(pg_store: Store) -> None:
    state = SearchState(Settings.from_env())
    assert state.fallback_toggle is False


def test_a_database_error_keeps_the_last_value_and_never_raises(
    open_app: OpenApp, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    clock = Clock()
    with open_app(models=False) as a, open_app(models=False) as b:
        _use_clock(b, clock)
        a.post("/api/admin/fallback", json={"on": True}, headers=AUTH)
        assert _mode(b)[0] is True

        def broken(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("database unreachable")

        monkeypatch.setattr("kb.store.open_store", broken)
        for _ in range(3):
            clock.now += 6
            assert b.get("/healthz").status_code == 200
            assert b.app.state.search.fallback_toggle is True
    warnings = [r for r in caplog.records if "cannot read the fallback toggle" in r.getMessage()]
    assert len(warnings) == 1  # logged once, not on every attempt


def test_the_cache_reads_the_database_at_most_every_five_seconds(
    pg_store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    state = SearchState(Settings.from_env(), clock=clock)
    reads: list[int] = []
    real = open_store

    def counting(settings: Any) -> Any:
        reads.append(1)
        return real(settings)

    monkeypatch.setattr("kb.store.open_store", counting)
    for _ in range(20):
        state.fallback_toggle
    assert len(reads) == 1
    clock.now += 5
    state.fallback_toggle
    assert len(reads) == 2
