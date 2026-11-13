"""Check a restored copy of the kb PostgreSQL database (phase 26, used by infra/restore-test-db.sh).

Usage:
    python -I infra/scripts/verify_restore.py --host HOST --password-file FILE
        [--port 5432] [--user kbadmin] [--dbname kb] [--sslmode require]

Passes (prints "RESTORE CHECK PASSED", exit 0) when the pgvector extension is
installed, the copy has documents and chunks, every chunk has an embedding, and
a cosine query over the restored vectors runs. Anything else, including a
connection failure or missing tables, prints the reason and
"RESTORE CHECK FAILED" (exit 1). The password is read from a file and never
printed. Needs psycopg (the project virtualenv).
"""

from __future__ import annotations

import argparse
import sys

import psycopg


def check(conn: psycopg.Connection) -> tuple[bool, str]:
    """Return (passed, summary) for an open connection to the restored copy."""

    def one(sql: str) -> object:
        row = conn.execute(sql).fetchone()
        return row[0] if row else None

    vector = one("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
    if vector is None:
        return False, "the pgvector extension is not installed"
    docs, chunks = one("SELECT count(*) FROM docs"), one("SELECT count(*) FROM chunks")
    embedded = one("SELECT count(*) FROM chunks WHERE embedding IS NOT NULL")
    latest = one("SELECT max(updated_at) FROM docs")
    summary = f"{docs} docs, {chunks} chunks, {embedded} embedded, pgvector {vector}, newest doc {latest}"
    if not docs or not chunks:
        return False, f"{summary}; the copy is empty"
    if embedded != chunks:
        return False, f"{summary}; some chunks have no embedding"
    # A real cosine query over the restored vectors, as search runs it.
    conn.execute(
        "SELECT c.id FROM chunks c, (SELECT embedding FROM chunks WHERE embedding IS NOT NULL LIMIT 1) s "
        "ORDER BY c.embedding <=> s.embedding LIMIT 5"
    ).fetchall()
    return True, summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check a restored kb PostgreSQL copy.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--user", default="kbadmin")
    parser.add_argument("--dbname", default="kb")
    parser.add_argument("--sslmode", default="require")
    parser.add_argument("--password-file", required=True)
    args = parser.parse_args(argv)
    try:
        with open(args.password_file, encoding="utf-8") as f:
            password = f.read().strip()
        with psycopg.connect(
            host=args.host, port=args.port, dbname=args.dbname, user=args.user,
            password=password, sslmode=args.sslmode, connect_timeout=15,
        ) as conn:
            passed, summary = check(conn)
    except (OSError, psycopg.Error) as exc:
        # The message names the problem (no table, no connection); it never contains the password.
        passed, summary = False, f"{type(exc).__name__}: {str(exc).strip().splitlines()[0] if str(exc).strip() else ''}"
    print(f"restored copy: {summary}")
    print("RESTORE CHECK PASSED" if passed else "RESTORE CHECK FAILED")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
