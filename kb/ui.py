"""Web UI pages and htmx fragments: search, doc, add, maintenance.

Server-rendered Jinja2, no build step. Reads call `kb.service` directly. The
write buttons post to `/ui/...` routes that check the same bearer token as the
REST API (`kb.auth.require_writer`), call the same service functions and
answer with an HTML fragment (htmx swaps HTML, and the JSON `/api` routes
cannot take a form post). See `kb.ui_add` for the add page.
"""

from __future__ import annotations


from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse

from kb import service
from kb.auth import require_writer
from kb.ratelimit import check_search_rate
from kb.schemas import SearchResult
from kb.ui_add import router as add_router
from kb.ui_common import Conn, Search, page, ui_errors
from kb.search.state import SearchState
from kb.store import Store

router = APIRouter(include_in_schema=False)
router.include_router(add_router)

STALE_DAYS = 90


async def _search(request: Request, search: SearchState, q: str) -> SearchResult | None:
    """Run the query (None for a blank one) after counting it against the search rate limit."""
    if not q.strip():
        return None
    check_search_rate(request)
    return await service.search(search, q)


@router.get("/", response_class=HTMLResponse)
@ui_errors
async def search_page(request: Request, q: str = Query(""), search: SearchState = Search) -> HTMLResponse:
    """The search page; with `?q=` the results are rendered into the page."""
    result = await _search(request, search, q)
    return page(request, "index.html", q=q, result=result)


@router.get("/ui/search", response_class=HTMLResponse)
@ui_errors
async def search_fragment(
    request: Request, q: str = Query(""), search: SearchState = Search
) -> HTMLResponse:
    """The results fragment htmx swaps in under the query box."""
    result = await _search(request, search, q)
    return page(request, "_results.html", q=q, result=result)


@router.get("/docs/{doc_id}", response_class=HTMLResponse)
@ui_errors
def doc_page(request: Request, doc_id: int, conn: Store = Conn) -> HTMLResponse:
    """One document: metadata, sanitized Markdown, and the Verify button."""
    return page(request, "doc.html", doc=service.get(conn, doc_id=doc_id))


@router.post("/ui/docs/{doc_id}/verify", response_class=HTMLResponse)
@ui_errors
def verify_fragment(
    request: Request, doc_id: int, view: str = Form("box"), conn: Store = Conn
) -> HTMLResponse:
    """Verify a doc as the calling token and return the new verified line.

    `view=cell` answers with the compact text used in the maintenance table,
    anything else with the box on the doc page.
    """
    actor = require_writer(request)
    result = service.verify(conn, doc_id, actor)
    name = "_verified_cell.html" if view == "cell" else "_verify_box.html"
    return page(request, name, doc_id=doc_id, verified_at=result.verified_at, verified_by=result.verified_by)


@router.get("/add", response_class=HTMLResponse)
def add_page(request: Request) -> HTMLResponse:
    """The add page with its three forms."""
    return page(request, "add.html")


@router.get("/maintenance", response_class=HTMLResponse)
@ui_errors
def maintenance_page(
    request: Request, days: int = Query(STALE_DAYS, ge=0, le=3650), conn: Store = Conn
) -> HTMLResponse:
    """Stale docs, top gaps, the last 20 audit rows and the fallback toggle."""
    return page(
        request,
        "maintenance.html",
        days=days,
        stale=service.stale(conn, days=days, limit=20).docs,
        gaps=service.gaps(conn, limit=20).gaps,
        audit=service.audit(conn, limit=20).entries,
    )


@router.post("/ui/admin/fallback", response_class=HTMLResponse)
@ui_errors
def fallback_fragment(
    request: Request,
    on: bool = Form(...),
    conn: Store = Conn,
    search: SearchState = Search,
) -> HTMLResponse:
    """Switch forced keyword search on or off and return the toggle box."""
    actor = require_writer(request)
    result = service.set_fallback(conn, search, on, actor)
    # `oob` also re-sends the header badge so it follows the toggle.
    return page(request, "_fallback_box.html", fallback=result.fallback_toggle, oob=True)
