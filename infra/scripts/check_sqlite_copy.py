"""Check a gzipped SQLite copy of the knowledge base (phase 26).

Usage: python3 -I infra/scripts/check_sqlite_copy.py FILE.db.gz

Prints the document count on stdout and a summary on stderr, and exits 0 when
the file is a readable gzip of an SQLite database that passes
`PRAGMA integrity_check` and has the `docs` and `chunks` tables. Exits 1 with
the reason on stderr otherwise. Standard library only, so `infra/up.sh save`
can run it with the system Python (no virtualenv, no kb package).
"""

from __future__ import annotations

import gzip
import shutil
import sqlite3
import sys
import tempfile
import zlib


def check(path: str) -> tuple[int, int]:
    """Return (docs, chunks) for a good copy; raise ValueError with the reason otherwise."""
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        try:
            with gzip.open(path, "rb") as src:
                shutil.copyfileobj(src, tmp)
        except (OSError, EOFError, zlib.error) as exc:
            raise ValueError(f"not a readable gzip file: {exc}") from exc
        tmp.flush()
        try:
            conn = sqlite3.connect(f"file:{tmp.name}?mode=ro", uri=True)
            try:
                rows = conn.execute("PRAGMA integrity_check").fetchall()
                if [r[0] for r in rows] != ["ok"]:
                    raise ValueError(f"integrity_check failed: {rows[0][0]}")
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
                missing = {"docs", "chunks"} - tables
                if missing:
                    raise ValueError(f"not a knowledge base: no {', '.join(sorted(missing))} table")
                docs = conn.execute("SELECT count(*) FROM docs").fetchone()[0]
                chunks = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
            finally:
                conn.close()
        except sqlite3.DatabaseError as exc:
            raise ValueError(f"not a readable SQLite database: {exc}") from exc
    return docs, chunks


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_sqlite_copy.py FILE.db.gz", file=sys.stderr)
        return 2
    try:
        docs, chunks = check(argv[1])
    except ValueError as exc:
        print(f"{argv[1]}: {exc}", file=sys.stderr)
        return 1
    print(f"{argv[1]}: integrity ok, {docs} docs, {chunks} chunks", file=sys.stderr)
    print(docs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
