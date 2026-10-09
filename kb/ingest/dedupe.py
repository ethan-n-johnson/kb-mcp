"""Exact-duplicate detection by content hash.

Near-duplicate (embedding similarity) checks live in `kb.search.dupes`.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import unicodedata

_INNER_SPACE_RE = re.compile(r"(?<=\S)[ \t]+")


def normalize_markdown(md: str) -> str:
    """Canonical form used for hashing.

    Applies Unicode NFC, unifies line endings, trims trailing whitespace,
    collapses runs of spaces and tabs inside a line to one space (leading
    indentation is kept so code stays distinct), collapses runs of blank lines
    to one and strips the whole text.
    """
    text = unicodedata.normalize("NFC", md).replace("\r\n", "\n").replace("\r", "\n")
    lines = [_INNER_SPACE_RE.sub(" ", line.rstrip()) for line in text.split("\n")]
    out: list[str] = []
    for line in lines:
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    return "\n".join(out).strip()


def content_hash(md: str) -> str:
    """SHA-256 hex digest of the normalized Markdown."""
    return hashlib.sha256(normalize_markdown(md).encode("utf-8")).hexdigest()


def find_exact_duplicate(conn: sqlite3.Connection, digest: str) -> int | None:
    """Return the id of the doc whose `content_hash` equals `digest`, if any."""
    row = conn.execute("SELECT id FROM docs WHERE content_hash = ?", (digest,)).fetchone()
    return int(row["id"]) if row else None
