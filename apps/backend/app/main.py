"""FastAPI application entry point."""

import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# Fix for Windows: Use ProactorEventLoop for subprocess support (Playwright)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

logger = logging.getLogger(__name__)
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.ai_budget import operation_error_content
from app.config import settings
from app.database import DatabaseBusyError, db
from app.mcp.http import (
    MCP_HTTP_METHODS,
    MCP_HTTP_PATH,
    MCP_HTTP_PATH_SLASH,
    mcp_http_endpoint,
    mcp_http_lifespan,
    validate_mcp_http_settings,
)
from app.pdf import close_pdf_renderer, init_pdf_renderer
from app.routers import (
    applications_router,
    config_router,
    enrichment_router,
    health_router,
    jobs_router,
    parse_check_router,
    resume_wizard_router,
    resumes_router,
)
from app.routers.resumes import drain_processing_cleanup_tasks


def _configure_application_logging() -> None:
    """Set application log level from configuration."""
    numeric_level = getattr(logging, settings.log_level, logging.INFO)
    logging.getLogger("app").setLevel(numeric_level)


_configure_application_logging()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan manager."""
    # Startup
    # Refuse unsafe MCP HTTP settings before touching any data.
    validate_mcp_http_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    # Import a legacy TinyDB database into SQLite if present (idempotent).
    # Fail-fast on error: starting with an empty DB would look like data loss.
    from app.scripts.migrate_tinydb_to_sqlite import migrate as migrate_tinydb

    result = await migrate_tinydb()
    if result.get("status") == "migrated":
        logger.info("Startup data migration: %s", result)
    # Fold any legacy plaintext API keys into the encrypted store (idempotent,
    # non-clobbering), then strip them from config.json.
    from app.config import migrate_legacy_keys

    migrate_legacy_keys()
    # PDF renderer uses lazy initialization - will initialize on first use
    # await init_pdf_renderer()
    # MCP over Streamable HTTP (off unless MCP_HTTP_ENABLED). Its tasks and
    # bridge close before the drains below; the drains run even if the MCP
    # teardown fails.
    try:
        async with mcp_http_lifespan(app):
            yield
    finally:
        # Shutdown - wrap each cleanup in try-except to ensure all resources are released
        try:
            await drain_processing_cleanup_tasks()
        except Exception:
            logger.exception("Error draining processing cleanup")

        try:
            await close_pdf_renderer()
        except Exception as e:
            logger.error(f"Error closing PDF renderer: {e}")

        try:
            await db.close()
        except Exception as e:
            logger.error(f"Error closing database: {e}")


app = FastAPI(
    title="Resume Matcher API",
    description="AI-powered resume tailoring for job descriptions",
    version=__version__,
    lifespan=lifespan,
)

@app.exception_handler(DatabaseBusyError)
async def database_busy_handler(request: Request, error: DatabaseBusyError) -> JSONResponse:
    logger.warning("Database write contention for %s", request.url.path, exc_info=error)
    return JSONResponse(
        status_code=503,
        content=operation_error_content(request, "Database is busy. Please retry shortly."),
        headers={"Retry-After": "1"},
    )


# CORS middleware - origins configurable via CORS_ORIGINS env var
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.effective_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(health_router, prefix="/api/v1")
app.include_router(config_router, prefix="/api/v1")
app.include_router(resumes_router, prefix="/api/v1")
app.include_router(jobs_router, prefix="/api/v1")
app.include_router(enrichment_router, prefix="/api/v1")
app.include_router(applications_router, prefix="/api/v1")
app.include_router(resume_wizard_router, prefix="/api/v1")
app.include_router(parse_check_router, prefix="/api/v1")

# Exact route (no Mount): a trailing-slash redirect would loop through the
# Next.js proxy. The endpoint resolves the current lifespan's session manager
# per request and answers 404 while MCP over HTTP is disabled.
for _mcp_path in (MCP_HTTP_PATH, MCP_HTTP_PATH_SLASH):
    app.add_route(_mcp_path, mcp_http_endpoint, methods=MCP_HTTP_METHODS, include_in_schema=False)


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "name": "Resume Matcher API",
        "version": __version__,
        "docs": "/docs",
    }


def main():
    """Entry point for the project.scripts console script."""
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.reload,
    )


if __name__ == "__main__":
    main()
