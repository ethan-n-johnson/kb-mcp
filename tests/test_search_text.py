"""Query text helpers and the metrics line."""

from __future__ import annotations

import io
import json

from kb.logging import setup_logging
from kb.metrics import emit_search_metrics
from kb.search.text import fts_match, normalize_query, query_terms, snippet_from


def test_query_terms_drop_stop_words_and_duplicates() -> None:
    assert query_terms("How do I attach the EBS volume? EBS!") == ["attach", "ebs", "volume"]
    assert query_terms("the and of") == ["the", "and", "of"]  # only stop words: keep them
    assert query_terms('"-*()') == []
    assert len(query_terms(" ".join(f"w{i}" for i in range(500)))) == 64


def test_fts_match_quotes_every_term() -> None:
    assert fts_match(["a", "and", "near"]) == '"a" OR "and" OR "near"'
    assert fts_match(["a", "b"], "AND") == '"a" AND "b"'
    assert fts_match([]) == ""
    assert fts_match(['we"ird']) == '"we""ird"'


def test_normalize_query() -> None:
    assert normalize_query("  How do I ATTACH an EBS-volume??  ") == "attach ebs volume"
    assert normalize_query("What is the") == "what is the"  # all stop words: unfiltered
    assert normalize_query("!!!") == ""


def test_snippet_cuts_at_word_boundary() -> None:
    assert snippet_from("short   text\nhere") == "short text here"
    text = "word " * 100
    snip = snippet_from(text)
    assert len(snip) <= 240 and snip.endswith("word") and text.startswith(snip)
    long_word = "x" * 500
    assert snippet_from(long_word) == "x" * 240
    # The cut never lands inside a word.
    mixed = ("abcdefghi " * 30).strip()
    assert snippet_from(mixed).split()[-1] == "abcdefghi"


def test_emit_search_metrics_writes_emf_line() -> None:
    buf = io.StringIO()
    setup_logging(buf)
    emit_search_metrics("keyword", "medium", 12.345)
    line = json.loads(buf.getvalue().strip().splitlines()[-1])
    emf = line["_aws"]["CloudWatchMetrics"][0]
    assert emf["Namespace"] == "KbMcp"
    assert emf["Dimensions"] == [["Mode"]]
    assert {m["Name"]: m["Unit"] for m in emf["Metrics"]} == {
        "LatencyMs": "Milliseconds",
        "ConfidenceLevel": "None",
        "KeywordSearches": "Count",
    }
    assert isinstance(line["_aws"]["Timestamp"], int)
    assert (line["Mode"], line["LatencyMs"], line["ConfidenceLevel"], line["KeywordSearches"]) == (
        "keyword",
        12.35,
        1,
        1,
    )
    emit_search_metrics("semantic", "high", 3.0)
    last = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert (last["ConfidenceLevel"], last["KeywordSearches"]) == (2, 0)
    emit_search_metrics("semantic", "low", 3.0)
    assert json.loads(buf.getvalue().strip().splitlines()[-1])["ConfidenceLevel"] == 0
