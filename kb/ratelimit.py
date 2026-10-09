"""Per-client rate limit for search, shared by REST, the UI and the MCP tool.

One bucket per client address covers `GET /api/search`, the UI search pages
(`/` with `?q=` and `/ui/search`) and the MCP `search` tool, so a caller cannot
triple the budget by switching interface. The limit is `KB_SEARCH_RATE`
(default `30/minute`). The engine is slowapi's `Limiter` (the `limits` library
with in-memory storage and a moving window); the decorator API is not used
because the MCP tool has no route to decorate. The state lives in this
process, which is correct because the service runs exactly one worker.

Stdio MCP has no request, so it is never limited.
"""

from __future__ import annotations

import functools
import math
import time

from limits import RateLimitItem, parse_many
from slowapi import Limiter
from starlette.requests import Request

from kb.config import get_settings
from kb.errors import ServiceError

RATE_LIMIT_MESSAGE = "Rate limit exceeded, try again in a minute"
SCOPE = "search"


def client_key(request: Request) -> str:
    """The client address the limit counts (`request.client.host`, see `kb.clientip`)."""
    return request.client.host if request.client else "unknown"


limiter = Limiter(
    key_func=client_key,
    strategy="moving-window",
    storage_uri="memory://",
    config_filename=None,
)


@functools.lru_cache(maxsize=8)
def parse_rate(value: str) -> RateLimitItem:
    """Parse a single rate such as `30/minute`; raises `ValueError` naming the bad value.

    Several limits in one string and a zero rate (which would refuse every
    search) are errors rather than being quietly reduced or accepted.
    """
    problem = f"invalid KB_SEARCH_RATE {value!r}: expected one rate such as 30/minute"
    try:
        items = parse_many(value)
    except Exception as exc:
        raise ValueError(problem) from exc
    if len(items) != 1 or items[0].amount < 1:
        raise ValueError(problem)
    return items[0]


def reset() -> None:
    """Forget every client's count (used by tests)."""
    limiter.reset()


def check_search_rate(request: Request | None) -> None:
    """Count one search for the caller; raise a 429 `ServiceError` when over the limit.

    `request=None` (stdio MCP) is unlimited. The error carries a `Retry-After`
    header for REST; the UI shows the message and MCP turns it into a tool error.
    """
    if request is None:
        return
    item = parse_rate(get_settings().search_rate)
    key = client_key(request)
    if limiter.limiter.hit(item, SCOPE, key):
        return
    stats = limiter.limiter.get_window_stats(item, SCOPE, key)
    wait = max(1, math.ceil(stats.reset_time - time.time()))
    raise ServiceError(
        "rate_limited", RATE_LIMIT_MESSAGE, 429, headers={"Retry-After": str(wait)}
    )
