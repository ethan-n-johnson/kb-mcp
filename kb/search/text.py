"""Query text helpers: tokenization, stop words, FTS5 query building, gap normalization."""

from __future__ import annotations

import re

_WORD_RE = re.compile(r"\w+", re.UNICODE)
MAX_TERMS = 64

STOP_WORDS = frozenset(
    """a an and are as at be but by can do does for from has have how i if in into is it
    its me my not of on or our so than that the their then there these they this to
    was we what when where which who why will with would you your""".split()
)


def words(text: str) -> list[str]:
    """Lowercase word tokens of `text` (letters, digits, underscore)."""
    return _WORD_RE.findall(text.lower())


def query_terms(query: str) -> list[str]:
    """Distinct search terms of a user query, in order, stop words removed.

    At most `MAX_TERMS` terms are kept so a pasted page cannot build a huge query.

    If the query is made only of stop words they are kept, so the search still
    has something to match.
    """
    all_words = list(dict.fromkeys(words(query)))
    content = [w for w in all_words if w not in STOP_WORDS]
    return (content or all_words)[:MAX_TERMS]


def fts_match(terms: list[str], joiner: str = "OR") -> str:
    """Build an FTS5 MATCH expression from terms, each quoted as a string.

    Quoting makes operators and punctuation inert (`AND`, `NEAR`, `*`, `-`, `"`
    cannot cause a syntax error). Returns an empty string when there are no terms.
    """
    return f" {joiner} ".join('"' + t.replace('"', '""') + '"' for t in terms)


def normalize_query(query: str) -> str:
    """Normal form used to group repeated questions in `gaps`.

    Lowercase, punctuation stripped, a small English stop-word list removed,
    whitespace collapsed. Falls back to the unfiltered words when every word
    is a stop word.
    """
    all_words = words(query)
    kept = [w for w in all_words if w not in STOP_WORDS]
    return " ".join(kept or all_words)


def snippet_from(text: str, limit: int = 240) -> str:
    """First `limit` characters of `text`, whitespace collapsed, cut at a word boundary."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    cut = flat[:limit]
    if flat[limit] != " ":
        head, sep, _ = cut.rpartition(" ")
        cut = head if sep else cut
    return cut.rstrip()
