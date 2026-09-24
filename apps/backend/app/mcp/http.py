"""Streamable HTTP transport for the MCP server at ``/api/v1/mcp``.

The endpoint is registered once at import time as an exact route and looks up
the session manager on ``app.state`` per request. Each app lifespan builds a
fresh server and session manager (``StreamableHTTPSessionManager.run()`` is
single-use), so a route that captured the first manager would break every
later lifespan.

Security layers, in order:

1. Disabled (``MCP_HTTP_ENABLED`` off, the default) -> 404.
2. Bearer token (``MCP_AUTH_TOKEN``), compared in constant time -> 401. This is
   the primary control: behind the Next.js proxy the backend always sees
   ``Host: 127.0.0.1:8000``, so host checks cannot tell local from remote
   callers.
3. Optional ``X-Forwarded-Host`` allow-list -> 421.
4. SDK transport security (Host -> 421, Origin -> 403) as defense in depth
   against DNS rebinding from a browser.
"""

import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from app.config import settings
from app.mcp.runtime import MCPRuntime
from app.mcp.server import build_mcp_server

logger = logging.getLogger(__name__)

MCP_HTTP_PATH = "/api/v1/mcp"
MCP_HTTP_METHODS = ["POST", "GET", "DELETE"]
# base64 of a 4 MB upload is ~5.6 MB, above the SDK's 4 MiB default.
MAX_REQUEST_BODY_BYTES = 8 * 1024 * 1024


class MCPHTTPConfigurationError(RuntimeError):
    """Raised at startup when the HTTP transport is enabled unsafely."""


def build_transport_security() -> TransportSecuritySettings:
    """Host/Origin validation settings from configuration."""
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(settings.mcp_allowed_hosts),
        allowed_origins=list(settings.mcp_allowed_origins),
    )


def _configured_token() -> bytes | None:
    """The configured bearer token as UTF-8 bytes, or None when unset."""
    token = settings.mcp_auth_token.get_secret_value().strip()
    return token.encode("utf-8") if token else None


@asynccontextmanager
async def mcp_http_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Serve MCP over HTTP for one app lifespan when enabled.

    Raises:
        MCPHTTPConfigurationError: If enabled without ``MCP_AUTH_TOKEN`` and
            without the explicit ``MCP_ALLOW_NO_AUTH`` opt-out.
    """
    app.state.mcp_session_manager = None
    app.state.mcp_auth_token = None
    if not settings.mcp_http_enabled:
        yield
        return

    token = _configured_token()
    if token is None:
        if not settings.mcp_allow_no_auth:
            message = (
                "MCP_HTTP_ENABLED is set but MCP_AUTH_TOKEN is empty. Set MCP_AUTH_TOKEN, "
                "or set MCP_ALLOW_NO_AUTH=1 to serve MCP without authentication (unsafe)."
            )
            logger.error(message)
            raise MCPHTTPConfigurationError(message)
        logger.warning(
            "MCP HTTP transport enabled WITHOUT authentication (MCP_ALLOW_NO_AUTH=1). "
            "Anyone who can reach %s can read and modify resumes and spend LLM credits.",
            MCP_HTTP_PATH,
        )

    runtime = MCPRuntime.create(app, transport="http")
    try:
        server = build_mcp_server(runtime)
        # Called for its side effect: it creates server.session_manager. The
        # returned Starlette app is unused; the exact route below serves it.
        server.streamable_http_app(
            stateless_http=True,
            json_response=True,
            max_request_body_size=MAX_REQUEST_BODY_BYTES,
            transport_security=build_transport_security(),
        )
        manager = server.session_manager
        async with manager.run():
            app.state.mcp_session_manager = manager
            app.state.mcp_auth_token = token
            logger.info("MCP Streamable HTTP transport serving at %s", MCP_HTTP_PATH)
            try:
                yield
            finally:
                app.state.mcp_session_manager = None
                app.state.mcp_auth_token = None
    finally:
        await runtime.aclose()


def _host_allowed(value: str, allowed: list[str]) -> bool:
    """Match ``value`` exactly or against ``host:*`` wildcard-port patterns."""
    for pattern in allowed:
        if value == pattern:
            return True
        if pattern.endswith(":*") and value.startswith(pattern[:-1]):
            return True
    return False


def _bearer_matches(headers: Headers, expected: bytes) -> bool:
    """Constant-time comparison of the request's bearer token.

    ASGI header values are decoded as latin-1, so re-encoding with latin-1
    recovers the raw bytes the client sent (UTF-8 for a UTF-8 token).
    """
    scheme, _, credentials = headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer":
        return False
    return hmac.compare_digest(credentials.strip().encode("latin-1"), expected)


class MCPHTTPEndpoint:
    """ASGI endpoint resolving the current lifespan's session manager per request."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        state = scope["app"].state
        manager: StreamableHTTPSessionManager | None = getattr(
            state, "mcp_session_manager", None
        )
        if manager is None:
            await JSONResponse({"detail": "Not Found"}, status_code=404)(scope, receive, send)
            return

        headers = Headers(scope=scope)
        expected: bytes | None = getattr(state, "mcp_auth_token", None)
        if expected is None:
            logger.warning("Serving unauthenticated MCP HTTP request (MCP_ALLOW_NO_AUTH=1)")
        elif not _bearer_matches(headers, expected):
            await JSONResponse(
                {"detail": "Unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )(scope, receive, send)
            return

        allowed_forwarded = settings.mcp_allowed_forwarded_hosts
        forwarded_host = headers.get("x-forwarded-host")
        if allowed_forwarded and forwarded_host is not None:
            hosts = [value.strip() for value in forwarded_host.split(",")]
            if not all(_host_allowed(value, allowed_forwarded) for value in hosts):
                logger.warning("Rejected MCP request with X-Forwarded-Host %r", forwarded_host[:256])
                await JSONResponse(
                    {"detail": "Invalid X-Forwarded-Host header"}, status_code=421
                )(scope, receive, send)
                return

        await StreamableHTTPASGIApp(manager)(scope, receive, send)


mcp_http_endpoint = MCPHTTPEndpoint()
