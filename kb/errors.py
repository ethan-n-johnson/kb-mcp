"""Errors shared by the service layer and every interface."""

from __future__ import annotations

# HTTP status for each `IngestError.code`. Codes not listed map to 422.
_INGEST_STATUS: dict[str, int] = {
    "bad_format": 400,
    "bad_scheme": 400,
    "blocked_address": 400,
    "too_large": 413,
    "fetch_failed": 502,
}


class ServiceError(Exception):
    """A service-level refusal with a stable code and an HTTP status.

    `code` is machine readable (`not_found`, `unauthorized`, `bad_request`, ...);
    `message` is safe to show to the caller; `http_status` is used by REST and
    ignored by interfaces that have no status codes; `headers` are extra
    response headers for REST (for example `Retry-After` on a 429).
    """

    def __init__(
        self,
        code: str,
        message: str,
        http_status: int = 400,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.http_status = http_status
        self.headers = headers or {}


def ingest_status(code: str) -> int:
    """HTTP status for an `IngestError` code (default 422)."""
    return _INGEST_STATUS.get(code, 422)
