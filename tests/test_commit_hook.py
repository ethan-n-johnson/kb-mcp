"""The commit hook in `kb.db`: which statements tell the listeners that a write was committed."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Iterator

import pytest

from kb import db

INSERT = "INSERT INTO audit_log (action, at) VALUES ('t', 'now')"


@pytest.fixture
def seen() -> Iterator[list[str]]:
    """Keys reported by the commit listener while the test runs."""
    keys: list[str] = []
    db.add_commit_listener(keys.append)
    yield keys
    db.remove_commit_listener(keys.append)


@pytest.fixture
def conn(db_path: Path, seen: list[str]) -> Iterator[sqlite3.Connection]:
    c = db.connect(db_path)
    db.init_schema(c)
    yield c
    c.close()


def test_creating_the_schema_is_not_a_write(conn: sqlite3.Connection, seen: list[str]) -> None:
    assert seen == []  # an empty database must not become a snapshot just because the app started


def test_the_key_is_the_resolved_database_path(conn: sqlite3.Connection, seen: list[str], db_path: Path) -> None:
    conn.execute(INSERT)
    assert seen == [str(db_path.resolve())] == [db.db_key(db_path)]


def test_autocommit_statements_are_reported_once_each(conn: sqlite3.Connection, seen: list[str]) -> None:
    conn.execute(INSERT)
    conn.execute("UPDATE audit_log SET note = 'x'")
    conn.execute("DELETE FROM audit_log")
    assert len(seen) == 3


def test_a_transaction_is_reported_at_commit_not_before(conn: sqlite3.Connection, seen: list[str]) -> None:
    with db.transaction(conn):
        conn.execute(INSERT)
        conn.execute(INSERT)
        assert seen == []  # not durable yet
    assert len(seen) == 1


def test_a_rolled_back_transaction_is_not_reported(conn: sqlite3.Connection, seen: list[str]) -> None:
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            conn.execute(INSERT)
            raise RuntimeError("boom")
    assert seen == []
    conn.execute(INSERT)  # the discarded count does not leak into the next write
    assert len(seen) == 1


def test_a_failed_statement_that_changed_nothing_is_not_reported(conn: sqlite3.Connection, seen: list[str]) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO audit_log (id, action, at) VALUES (1, NULL, 'now')")
    assert seen == []


def test_executemany_executescript_and_cursors_are_reported(conn: sqlite3.Connection, seen: list[str]) -> None:
    conn.executemany("INSERT INTO audit_log (action, at) VALUES (?, 'now')", [("a",), ("b",)])
    assert len(seen) >= 1
    seen.clear()
    conn.executescript("INSERT INTO audit_log (action, at) VALUES ('c', 'now');")
    assert len(seen) == 1
    seen.clear()
    cur = conn.cursor()
    cur.execute(INSERT)
    assert len(seen) == 1
    seen.clear()
    cur.executemany("INSERT INTO audit_log (action, at) VALUES (?, 'now')", [("d",)])
    assert len(seen) == 1
    seen.clear()
    cur.executescript("DELETE FROM audit_log;")
    assert len(seen) == 1


def test_changes_made_by_triggers_and_cascades_are_covered(conn: sqlite3.Connection, seen: list[str]) -> None:
    conn.execute(
        "INSERT INTO docs (id, title, content_md, content_hash, created_at, updated_at)"
        " VALUES (1, 't', 'c', 'h', 'now', 'now')"
    )
    conn.execute("INSERT INTO chunks (doc_id, ord, text) VALUES (1, 0, 'hello')")
    seen.clear()
    conn.execute("DELETE FROM docs WHERE id = 1")  # cascades to chunks and fires the FTS trigger
    assert len(seen) == 1


def test_reads_and_no_op_writes_are_silent(conn: sqlite3.Connection, seen: list[str]) -> None:
    conn.execute(INSERT)
    seen.clear()
    conn.execute("SELECT * FROM audit_log").fetchall()
    conn.execute("UPDATE audit_log SET note = 'x' WHERE id = 99999")  # matches no row
    conn.execute("DELETE FROM audit_log WHERE id = 99999")
    conn.execute("PRAGMA integrity_check").fetchall()
    assert seen == []


def test_every_connection_from_connect_reports_not_only_the_first(db_path: Path, seen: list[str]) -> None:
    first, second = db.connect(db_path), db.connect(db_path)
    db.init_schema(first)
    first.execute(INSERT)
    second.execute(INSERT)
    first.close()
    second.close()
    assert len(seen) == 2


def test_a_broken_listener_cannot_fail_the_write(conn: sqlite3.Connection, seen: list[str]) -> None:
    def broken(key: str) -> None:
        raise RuntimeError("listener bug")

    db.add_commit_listener(broken)
    try:
        conn.execute(INSERT)  # does not raise
    finally:
        db.remove_commit_listener(broken)
    assert len(seen) == 1  # later listeners still run


def test_writes_from_many_threads_are_all_reported(db_path: Path, seen: list[str]) -> None:
    setup = db.connect(db_path)
    db.init_schema(setup)
    setup.close()

    def write() -> None:
        c = db.connect(db_path)
        try:
            for _ in range(5):
                c.execute(INSERT)
        finally:
            c.close()

    threads = [threading.Thread(target=write) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(seen) == 20


def test_row_factory_and_pragmas_are_unchanged(conn: sqlite3.Connection) -> None:
    conn.execute(INSERT)
    row = conn.execute("SELECT action FROM audit_log").fetchone()
    assert row["action"] == "t"
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
