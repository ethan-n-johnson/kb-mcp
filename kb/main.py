"""FastAPI application: lifespan, health check, the REST router and `/mcp`."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import Response
from starlette.types import Scope

from kb import snapshot
from kb.store import Store, close_pools, init_schema, open_store
from kb.api import register_error_handlers, router as api_router
from kb.clientip import ClientIpMiddleware
from kb.config import Settings, get_settings
from kb.logging import setup_logging
from kb.mcp_http import mount_mcp
from kb.mcp_tools import build_mcp
from kb.ratelimit import parse_rate
from kb.security import (
    CORS_HEADERS,
    CORS_METHODS,
    MAX_BODY_BYTES,
    BodySizeLimitMiddleware,
    SecurityHeadersMiddleware,
    cors_origin,
)
from kb.search import indexing
from kb.search.state import EmbedderLike, RerankerLike
from kb.ui import router as ui_router
from kb.ui_common import STATIC_DIR

# Configure JSON logging at import time so uvicorn's own startup lines (emitted
# after it imports this module, before the lifespan runs) are JSON too.
setup_logging()
log = logging.getLogger(__name__)

STATIC_MAX_AGE = 300


class CachedStaticFiles(StaticFiles):
    """`StaticFiles` that sends a short `Cache-Control`, so CloudFront can cache `/static/*`."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = f"public, max-age={STATIC_MAX_AGE}"
        return response


def _count_chunks(settings: Settings) -> int:
    """Chunk count on a short-lived connection (PostgreSQL mode; runs in a worker thread)."""
    conn = open_store(settings)
    try:
        return int(conn.execute("SELECT count(*) FROM chunks").fetchone()[0])
    finally:
        conn.close()


def _add_security(app: FastAPI) -> None:
    """Install the hardening middleware. Each `add_middleware` call wraps the previous ones.

    Outermost first: client address, security headers, CORS, body size limit,
    then the routes. CORS sits outside the size limit so a 413 still carries the
    CORS headers the extension needs to read it. Without `KB_EXTENSION_ORIGIN`
    there is no CORS middleware at all, so no CORS header is ever sent.
    """
    settings = get_settings()
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_BODY_BYTES)
    if settings.extension_origin:
        origin = cors_origin(settings.extension_origin)
        if origin is None:
            log.error("KB_EXTENSION_ORIGIN is not a single origin, CORS stays off")
        else:
            app.add_middleware(
                CORSMiddleware,
                allow_origins=[origin],
                allow_methods=CORS_METHODS,
                allow_headers=CORS_HEADERS,
                allow_credentials=False,
            )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(ClientIpMiddleware, hops=settings.proxy_hops)


def create_app(
    embedder: EmbedderLike | None = None, reranker: RerankerLike | None = None
) -> FastAPI:
    """Build the application.

    `embedder` and `reranker` inject model objects (the doubles in
    `tests/fakes.py`); by default the lifespan builds the real ones, or none at
    all when `KB_MODELS=off`. Injected models are used only when models are on,
    and the same startup path loads them, backfills vectors and builds the index.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Open the database, create the schema, start search, store state on `app.state`."""
        setup_logging()
        settings = get_settings()
        parse_rate(settings.search_rate)  # a bad KB_SEARCH_RATE stops startup, not every search
        conn: Store | None = open_store(settings)
        init_schema(conn)
        app.state.settings = settings
        if settings.dialect == "postgres":
            # No connection is held for /healthz: the pool serves it per request.
            conn.close()
            conn = None
        app.state.conn = conn
        # Started before the backfill so the vectors it writes are snapshotted too.
        snapshots = snapshot.from_settings(settings)
        app.state.snapshots = snapshots
        if snapshots is not None:
            snapshots.start()
        try:
            app.state.search = indexing.build_state(settings, embedder, reranker)
            await indexing.start(app.state.search)
            log.info(
                "startup complete",
                extra={
                    # The PostgreSQL URL carries the password; log only the dialect.
                    "database": settings.db_path if conn is not None else "postgres",
                    "models_on": settings.models_on,
                    "snapshots": snapshots is not None,
                },
            )
            # A mounted sub-app's lifespan is not run by Starlette, so the MCP
            # session manager (its task group) is started here.
            async with mcp.session_manager.run():
                yield
        finally:
            if snapshots is not None:
                # SIGTERM from `docker stop` lands here once uvicorn has drained
                # its requests: upload a last snapshot if a write is still pending.
                await run_in_threadpool(snapshots.stop)
            if conn is not None:
                conn.close()
            close_pools()
            log.info("shutdown complete")

    # Swagger UI and ReDoc load their scripts from a CDN, which the
    # Content-Security-Policy (script-src 'self') blocks; `/openapi.json` stays.
    app = FastAPI(title="kb-mcp", lifespan=lifespan, docs_url=None, redoc_url=None)
    register_error_handlers(app)
    app.include_router(api_router)
    app.include_router(ui_router)
    app.mount("/static", CachedStaticFiles(directory=STATIC_DIR), name="static")
    # The search state is created in the lifespan, so the tools look it up lazily.
    mcp = build_mcp(lambda: app.state.search)
    mount_mcp(app, mcp)
    _add_security(app)

    @app.get("/healthz")
    async def healthz(request: Request) -> JSONResponse:
        """Return 200 with status JSON whenever the database is readable, else 503.

        `mode` is `semantic` only when the toggle is off and the models are
        healthy; keyword mode is still a healthy state (200), so a model failure
        never makes the load balancer replace the instance.
        """
        state = request.app.state.search
        try:
            shared = request.app.state.conn
            if shared is not None:
                count = shared.execute("SELECT count(*) FROM chunks").fetchone()[0]
            else:
                count = await run_in_threadpool(_count_chunks, request.app.state.settings)
        except Exception:
            log.exception("health check failed")
            return JSONResponse({"status": "error"}, status_code=503)
        return JSONResponse(
            {
                "status": "ok",
                "mode": state.mode,
                "models": state.model_status(),
                "fallback_toggle": state.fallback_toggle,
                "chunks": count,
                "index_size": len(state.index),
            }
        )

    return app


app = create_app()
