"""Shared pieces of the web UI routers: templates, rendering, error pages, dependencies.

The UI holds no business logic. Routes read with `kb.service` and write through
the same service calls the REST routes use, behind the same bearer-token check;
they only turn results and errors into HTML.
"""

from __future__ import annotations

import functools
import inspect
import logging
from pathlib import Path
from typing import Any, Callable, TypeVar

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.templating import Jinja2Templates

from kb.api import get_conn, get_search
from kb.errors import ServiceError, ingest_status
from kb.ingest.errors import IngestError
from kb.render import render_markdown, render_snippet

log = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).parent
TEMPLATE_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"

Conn = Depends(get_conn)
Search = Depends(get_search)


def safe_url(value: str | None) -> str:
    """The URL if it is http(s), else an empty string (links to anything else are not rendered)."""
    if value and value.lower().startswith(("http://", "https://")):
        return value
    return ""


def short_date(value: str | None) -> str:
    """`2026-10-07T12:00:00Z` -> `2026-10-07`; empty for None."""
    return (value or "")[:10]


def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"], default=True),
    )
    env.filters["markdown"] = render_markdown
    env.filters["snippet"] = render_snippet
    env.filters["safe_url"] = safe_url
    env.filters["short_date"] = short_date
    return env


templates = Jinja2Templates(env=_environment())


def is_htmx(request: Request) -> bool:
    """True for requests sent by htmx (they want a fragment, not a page)."""
    return request.headers.get("hx-request") == "true"


def page(request: Request, name: str, status_code: int = 200, **context: Any) -> HTMLResponse:
    """Render template `name` with the header state (mode badge, fallback flag) added."""
    state = request.app.state.search
    context.setdefault("mode", state.mode)
    context.setdefault("fallback", state.fallback_toggle)
    return templates.TemplateResponse(request, name, context, status_code=status_code)


def error_response(request: Request, status: int, code: str, message: str) -> HTMLResponse:
    """An error as a notice fragment for htmx requests, else as a full error page."""
    name = "_error.html" if is_htmx(request) else "error.html"
    return page(request, name, status_code=status, code=code, message=message)


F = TypeVar("F", bound=Callable[..., Any])


def ui_errors(func: F) -> F:
    """Turn `ServiceError` and `IngestError` raised by a UI route into HTML errors.

    The status code is the one REST would use (401, 404, 409, 422, ...). htmx is
    told to swap error responses in (see `app.js`), so the notice shows where
    the result would have gone.
    """

    def handle(request: Request, exc: ServiceError | IngestError) -> HTMLResponse:
        if isinstance(exc, ServiceError):
            return error_response(request, exc.http_status, exc.code, exc.message)
        return error_response(request, ingest_status(exc.code), exc.code, exc.message)

    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(request: Request, *args: Any, **kwargs: Any) -> Any:
            try:
                return await func(request, *args, **kwargs)
            except (ServiceError, IngestError) as exc:
                return handle(request, exc)

        return async_wrapper  # type: ignore[return-value]

    @functools.wraps(func)
    def wrapper(request: Request, *args: Any, **kwargs: Any) -> Any:
        try:
            return func(request, *args, **kwargs)
        except (ServiceError, IngestError) as exc:
            return handle(request, exc)

    return wrapper  # type: ignore[return-value]

