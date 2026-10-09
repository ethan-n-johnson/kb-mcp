"""JSON-lines logging: one JSON object per log record."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any, TextIO

# Attributes present on every LogRecord; anything else came from `extra=`.
_STANDARD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}


_HANDLER_NAME = "kb-json"


class JsonFormatter(logging.Formatter):
    """Format records as a single-line JSON object with ts, level, logger, msg."""

    def format(self, record: logging.LogRecord) -> str:
        """Return the record as one JSON line, including any `extra` fields.

        `_aws` is the one underscore key kept: CloudWatch Embedded Metric Format
        lines (`kb.metrics`) need it at the top level.
        """
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and (not key.startswith("_") or key == "_aws"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def setup_logging(stream: TextIO | None = None, level: int = logging.INFO) -> None:
    """Configure the root logger to emit JSON lines to `stream` (default stdout).

    Stdio MCP mode passes `sys.stderr` because stdout carries the protocol.
    Safe to call more than once: the earlier JSON handler is replaced.
    """
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.set_name(_HANDLER_NAME)
    root = logging.getLogger()
    # Replace only our own earlier handler so foreign ones (pytest capture) survive.
    for old in list(root.handlers):
        if old.get_name() == _HANDLER_NAME:
            root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(level)
    # Uvicorn's own loggers should flow through the root JSON handler.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
