"""In-process HTTP client for calling the app's own API routes.

Features that must behave exactly like a route (the PDF download, the resume
payload the print page renders) call that route through
``httpx.ASGITransport`` instead of duplicating its logic. Requests never
leave the process; validation, time budgets and error translation stay the
route's own.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

INTERNAL_BASE_URL = "http://internal.local"
API_PREFIX = "/api/v1"

# Resume, job and application ids are UUID4 strings. Anything interpolated
# into a route path must be a single safe segment: httpx normalizes ``..``
# and treats ``?``/``#`` as delimiters, so a raw id could re-target another
# route.
PATH_SEGMENT_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")
# Every internal route path: one or more safe segments, nothing else.
ROUTE_PATH_PATTERN = re.compile(r"(?:/[A-Za-z0-9_-]+)+")

GENERIC_SERVER_ERROR = "Resume Matcher failed to complete the request. Please try again."


class InvalidIdentifierError(ValueError):
    """Raised when an id cannot be used as a single URL path segment."""


class InternalRequestError(Exception):
    """A non-2xx route response, carrying the route's client-safe detail."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def path_segment(value: str, name: str = "id") -> str:
    """Return ``value`` if it is a safe single path segment.

    Raises:
        InvalidIdentifierError: If ``value`` is empty, too long, or contains
            anything other than letters, digits, ``-`` or ``_``.
    """
    if not isinstance(value, str) or PATH_SEGMENT_PATTERN.fullmatch(value) is None:
        raise InvalidIdentifierError(f"Invalid {name}.")
    return value


def _error_message(response: httpx.Response) -> str:
    """Extract the route's client-safe ``detail`` without leaking internals."""
    try:
        body = response.json()
    except ValueError:
        body = None
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str) and detail.strip():
        return detail
    if response.status_code >= 500:
        return GENERIC_SERVER_ERROR
    return f"Request failed with status {response.status_code}."


class _ExceptionLoggingApp:
    """ASGI shim that logs unhandled route exceptions before re-raising.

    ``ASGITransport(raise_app_exceptions=False)`` turns them into bare 500
    responses without logging; this keeps the traceback in the server log.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await self._app(scope, receive, send)
        except Exception:
            logger.exception(
                "Unhandled exception in internal request %s %s",
                scope.get("method"),
                scope.get("path"),
            )
            raise


class InternalClient:
    """HTTP client bound to the FastAPI app through ``httpx.ASGITransport``."""

    def __init__(self, app: ASGIApp) -> None:
        # Routes bound their own work, so the client applies no timeout;
        # callers wrap requests in their own deadlines.
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=_ExceptionLoggingApp(app), raise_app_exceptions=False
            ),
            base_url=INTERNAL_BASE_URL,
            timeout=None,
        )

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def request(
        self, method: str, path: str, *, params: dict[str, Any] | None = None
    ) -> httpx.Response:
        """Send a request to ``/api/v1{path}`` and return the 2xx response.

        Raises:
            InvalidIdentifierError: If ``path`` is not made of safe segments.
            InternalRequestError: For any non-2xx response.
        """
        if ROUTE_PATH_PATTERN.fullmatch(path) is None:
            raise InvalidIdentifierError("Invalid route path.")
        response = await self._client.request(method, f"{API_PREFIX}{path}", params=params)
        if response.is_success:
            return response
        if response.status_code >= 500:
            logger.warning("Internal %s %s returned %s", method, path, response.status_code)
        raise InternalRequestError(_error_message(response), response.status_code)

    async def get_json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        """GET a route and decode its JSON body."""
        return (await self.request("GET", path, params=params)).json()
