"""JSON-lines logging."""

from __future__ import annotations

import io
import json
import logging

from kb.logging import setup_logging


def test_json_lines_with_extras() -> None:
    buf = io.StringIO()
    setup_logging(buf)
    logging.getLogger("kb.test").info("hello %s", "world", extra={"doc_id": 7})
    try:
        raise ValueError("x")
    except ValueError:
        logging.getLogger("kb.test").exception("failed")
    lines = buf.getvalue().strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["msg"] == "hello world"
    assert first["level"] == "INFO"
    assert first["logger"] == "kb.test"
    assert first["doc_id"] == 7
    assert "ts" in first
    assert "ValueError" in json.loads(lines[1])["exc"]


def test_setup_twice_does_not_duplicate() -> None:
    buf = io.StringIO()
    setup_logging(buf)
    setup_logging(buf)
    logging.getLogger("kb.test").info("once")
    assert len(buf.getvalue().strip().splitlines()) == 1
