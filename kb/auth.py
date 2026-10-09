"""Bearer-token checks: constant-time compare and the FastAPI write dependency."""

from __future__ import annotations

import hmac

from fastapi import Request

from kb import config
from kb.errors import ServiceError


def check_token(raw: str | None) -> str | None:
    """Return the name of the write token whose secret equals `raw`, else None.

    Every configured token is compared with `hmac.compare_digest` (no early
    exit on the first mismatch, so timing does not reveal which entry was
    closest). Secrets are never logged.
    """
    if not raw:
        return None
    candidate = raw.encode("utf-8")
    matched: str | None = None
    for name, secret in config.load_write_tokens().items():
        if hmac.compare_digest(candidate, secret.encode("utf-8")):
            matched = name
    return matched


def bearer_secret(header: str | None) -> str | None:
    """Extract the secret from an `Authorization: Bearer <secret>` header value."""
    if not header:
        return None
    scheme, _, secret = header.partition(" ")
    if scheme.lower() != "bearer" or not secret.strip():
        return None
    return secret.strip()


def require_writer(request: Request) -> str:
    """FastAPI dependency: return the token name or raise a 401 `ServiceError`.

    The API layer turns the 401 into JSON with a `WWW-Authenticate: Bearer`
    header. The message is the same for missing and wrong tokens.
    """
    name = check_token(bearer_secret(request.headers.get("authorization")))
    if name is None:
        raise ServiceError("unauthorized", "a valid bearer token is required", 401)
    return name
