"""stdio entry point: ``uv run resume-matcher-mcp`` or ``python -m app.mcp``.

Runs inside the FastAPI lifespan so startup migrations run and shutdown drains
background work and closes the database. stdout carries only JSON-RPC; all
logging goes to stderr.
"""

import logging
import sys

import anyio

from app.config import settings


def _configure_stderr_logging() -> None:
    """Route every log record to stderr; stdout is the protocol channel."""
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )


async def _main() -> None:
    """Serve MCP over stdio for the lifetime of the client connection."""
    from app.main import app
    from app.mcp.runtime import MCPRuntime
    from app.mcp.server import build_mcp_server

    # This process speaks stdio only; never start the HTTP transport here.
    # Mutating the settings singleton is intentionally process-wide: this
    # process is dedicated to one stdio client.
    settings.mcp_http_enabled = False
    async with app.router.lifespan_context(app):
        runtime = MCPRuntime.create(app, transport="stdio")
        server = build_mcp_server(runtime)
        try:
            await server.run_stdio_async()
        finally:
            await runtime.aclose()


def main() -> None:
    """Console-script entry point for ``resume-matcher-mcp``."""
    _configure_stderr_logging()
    # MCPServer.run() starts its own event loop and cannot be nested inside
    # the lifespan, so drive the async entry point directly.
    anyio.run(_main)


if __name__ == "__main__":
    main()
