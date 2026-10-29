"""The `Store` interface on SQLite and PostgreSQL (`dual`: both legs), plus the SQL translation."""

from __future__ import annotations

import re
import threading
import time

import numpy as np
import pytest

from kb import db
from kb.store import Store
from kb.store.postgres import adapt_params, translate

pytestmark = pytest.mark.dual

ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _doc(conn: Store, title: str = "T", digest: str = "h1") -> int:
    now = db.utcnow()
    return conn.insert(
        "INSERT INTO docs (title, source_type, content_md, content_hash, created_at, updated_at)"
        " VALUES (?, 'markdown', 'body', ?, ?, ?)",
        (title, digest, now, now),
    )


def _chunk(conn: Store, doc_id: int, text: str, heading: str = "H", ord_: int = 0) -> int:
    return conn.insert(
        "INSERT INTO chunks (doc_id, ord, heading_path, text) VALUES (?, ?, ?, ?)",
        (doc_id, ord_, heading, text),
    )


def test_insert_returns_increasing_ids(conn: Store) -> None:
    first, second = _doc(conn, digest="a"), _doc(conn, digest="b")
    assert (first, second) == (1, 2)


def test_rows_answer_names_positions_and_keys(conn: Store) -> None:
    _doc(conn, "Title")
    row = conn.fetchone("SELECT id, title FROM docs")
    assert (row["id"], row["title"]) == (1, "Title")
    assert (row[0], row[1]) == (1, "Title")
    assert list(row.keys()) == ["id", "title"]
    assert dict(row) == {"id": 1, "title": "Title"}
    assert tuple(row) == (1, "Title")


def test_fetchone_returns_none_and_fetchall_a_list(conn: Store) -> None:
    assert conn.fetchone("SELECT id FROM docs") is None
    _doc(conn)
    assert [r["id"] for r in conn.fetchall("SELECT id FROM docs")] == [1]
    assert [r["id"] for r in conn.execute("SELECT id FROM docs")] == [1]


def test_rowcount_of_update_and_delete(conn: Store) -> None:
    _doc(conn)
    assert conn.execute("UPDATE docs SET title = ? WHERE id = ?", ("x", 1)).rowcount == 1
    assert conn.execute("UPDATE docs SET title = ? WHERE id = ?", ("x", 99)).rowcount == 0
    assert conn.execute("DELETE FROM docs WHERE id = ?", (1,)).rowcount == 1


def test_insert_or_ignore_skips_a_duplicate(conn: Store) -> None:
    doc = _doc(conn)
    rows = [(doc, "a"), (doc, "a"), (doc, "b")]
    conn.executemany("INSERT OR IGNORE INTO doc_tags (doc_id, tag) VALUES (?, ?)", rows)
    assert [r["tag"] for r in conn.fetchall("SELECT tag FROM doc_tags ORDER BY tag")] == ["a", "b"]


def test_timestamps_come_back_as_iso_utc_strings(conn: Store) -> None:
    _doc(conn)
    row = conn.fetchone("SELECT created_at, verified_at FROM docs")
    assert ISO.match(row["created_at"]) and row["verified_at"] is None
    conn.execute("UPDATE docs SET verified_at = ?", ("2026-01-02T03:04:05Z",))
    assert conn.fetchone("SELECT verified_at FROM docs")["verified_at"] == "2026-01-02T03:04:05Z"
    # compared as time on both sides
    assert conn.fetchone("SELECT count(*) AS n FROM docs WHERE verified_at <= ?", ("2026-02-01T00:00:00Z",))["n"] == 1


def test_embedding_bytes_round_trip_exactly(conn: Store) -> None:
    doc = _doc(conn)
    vec = np.random.default_rng(7).standard_normal(384).astype(np.float32)
    vec /= np.linalg.norm(vec)
    blob = vec.tobytes()
    cid = _chunk(conn, doc, "text")
    conn.execute("UPDATE chunks SET embedding = ?, embed_model = ? WHERE id = ?", (blob, "m", cid))
    row = conn.fetchone("SELECT embedding, embed_model FROM chunks WHERE id = ?", (cid,))
    assert bytes(row["embedding"]) == blob and row["embed_model"] == "m"
    # a missing vector stays NULL
    other = _chunk(conn, doc, "more", ord_=1)
    assert conn.fetchone("SELECT embedding FROM chunks WHERE id = ?", (other,))["embedding"] is None


def test_is_not_param_is_null_safe(conn: Store) -> None:
    doc = _doc(conn)
    _chunk(conn, doc, "a")
    count = conn.fetchone("SELECT count(*) AS n FROM chunks WHERE embed_model IS NOT ?", ("m",))["n"]
    assert count == 1  # NULL is "not m"


def test_transaction_commits_and_rolls_back(conn: Store) -> None:
    with db.transaction(conn):
        _doc(conn, digest="kept")
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            _doc(conn, digest="lost")
            raise RuntimeError("boom")
    assert [r["content_hash"] for r in conn.fetchall("SELECT content_hash FROM docs")] == ["kept"]


def test_foreign_keys_cascade_on_delete(conn: Store) -> None:
    doc = _doc(conn)
    _chunk(conn, doc, "x")
    conn.execute("INSERT INTO doc_tags (doc_id, tag) VALUES (?, ?)", (doc, "t"))
    conn.execute("DELETE FROM docs WHERE id = ?", (doc,))
    assert conn.fetchone("SELECT count(*) AS n FROM chunks")["n"] == 0
    assert conn.fetchone("SELECT count(*) AS n FROM doc_tags")["n"] == 0


def test_write_transactions_do_not_overlap(conn: Store, db_path, backend: str) -> None:
    """A second writer waits until the first transaction has committed."""
    from tests.helpers import reopen

    events: list[str] = []
    entered = threading.Event()

    def holder() -> None:
        with db.transaction(conn):
            events.append("a in")
            entered.set()
            time.sleep(0.4)
            events.append("a out")

    other = reopen(db_path)
    thread = threading.Thread(target=holder)
    thread.start()
    assert entered.wait(5)
    with db.transaction(other):
        events.append("b in")
    thread.join()
    other.close()
    assert events == ["a in", "a out", "b in"]


# --- keyword search ------------------------------------------------------------------


def _corpus(conn: Store) -> tuple[int, int]:
    a, b = _doc(conn, "Load balancing", "ha"), _doc(conn, "Storage", "hb")
    _chunk(conn, a, "An Application Load Balancer distributes incoming traffic across targets.", "ELB")
    _chunk(conn, b, "Amazon S3 stores objects in buckets with several storage classes.", "S3")
    conn.execute("INSERT INTO doc_tags (doc_id, tag) VALUES (?, ?)", (b, "storage"))
    return a, b


def test_fts_rows_rank_and_shape(conn: Store) -> None:
    a, _ = _corpus(conn)
    rows = conn.fts_rows(["load", "balancer"], [], 5, with_snippet=True)
    assert [r["doc_id"] for r in rows] == [a]
    row = rows[0]
    assert row["title"] == "Load balancing" and row["heading_path"] == "ELB"
    assert row["score"] > 0 and "<b>" in row["snip"]
    assert conn.fts_rows(["balancer"], [], 5, with_snippet=False)[0]["snip"] == ""


def test_fts_rows_without_terms_or_matches_are_empty(conn: Store) -> None:
    _corpus(conn)
    assert conn.fts_rows([], [], 5, with_snippet=False) == []
    assert conn.fts_rows(["zebra"], [], 5, with_snippet=False) == []


def test_fts_rows_use_stemming_and_or_semantics(conn: Store) -> None:
    a, b = _corpus(conn)
    found = {r["doc_id"] for r in conn.fts_rows(["bucket", "balancing"], [], 5, with_snippet=False)}
    assert found == {a, b}


def test_fts_rows_tag_filter(conn: Store) -> None:
    a, b = _corpus(conn)
    rows = conn.fts_rows(["load", "balancer", "bucket"], ["storage"], 5, with_snippet=False)
    assert [r["doc_id"] for r in rows] == [b]


def test_fts_covers_needs_every_term(conn: Store) -> None:
    a, b = _corpus(conn)
    chunk = conn.fetchone("SELECT id FROM chunks WHERE doc_id = ?", (a,))["id"]
    assert conn.fts_covers(["load", "balancer"], chunk)
    assert not conn.fts_covers(["load", "bucket"], chunk)
    assert not conn.fts_covers([], chunk)


# --- translation (no database) --------------------------------------------------------


def test_translate_placeholders_and_percent() -> None:
    assert translate("SELECT 1 WHERE a = ? AND b LIKE '50%'") == "SELECT 1 WHERE a = %s AND b LIKE '50%%'"


def test_translate_insert_or_ignore() -> None:
    sql = translate("INSERT OR IGNORE INTO doc_tags (doc_id, tag) VALUES (?, ?)")
    assert sql == "INSERT INTO doc_tags (doc_id, tag) VALUES (%s, %s) ON CONFLICT DO NOTHING"


def test_translate_is_not_param() -> None:
    assert translate("WHERE m IS NOT ? AND n IS NOT NULL") == "WHERE m IS DISTINCT FROM %s AND n IS NOT NULL"


def test_adapt_params_vectors_and_nul() -> None:
    vec = np.arange(4, dtype=np.float32)
    out = adapt_params([vec.tobytes(), "a\x00b", 3, None])
    assert isinstance(out[0], np.ndarray) and out[0].tolist() == [0.0, 1.0, 2.0, 3.0]
    assert out[1:] == ["ab", 3, None]
