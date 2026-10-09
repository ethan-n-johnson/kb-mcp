"""Typed error raised by every ingestion refusal."""

from __future__ import annotations


class IngestError(Exception):
    """An ingestion step refused its input.

    `code` is a stable machine-readable string (for example `blocked_address`,
    `bad_scheme`, `too_large`, `fetch_failed`, `empty_content`, `no_text_layer`,
    `too_many_pages`); `message` is safe to show to the caller.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
