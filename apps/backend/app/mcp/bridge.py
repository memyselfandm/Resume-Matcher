"""In-process ASGI bridge from MCP tools to the FastAPI routers.

Tools call the same HTTP routes the web UI uses, so validation, time budgets,
error translation and side effects (tracker auto-creation, preview claims)
stay identical without touching router code. Requests never leave the process.

The transport, id validation and error extraction are the neutral
``app.internal_client``; this module only maps its errors to MCP tool errors
and adds the upload guards.
"""

import logging
from pathlib import Path
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError
from starlette.types import ASGIApp

from app import internal_client
from app.internal_client import (  # noqa: F401 - GENERIC_SERVER_ERROR, _error_message re-exported
    GENERIC_SERVER_ERROR,
    InternalClient,
    InternalRequestError,
    _error_message,
)

# The upload router's accepted extensions and raw size limit, imported so the
# pre-send checks can never drift from the route's own validation.
from app.routers.resumes import DOCUMENT_TYPES_BY_EXTENSION as UPLOAD_CONTENT_TYPES
from app.routers.resumes import MAX_FILE_SIZE as MAX_UPLOAD_BYTES

logger = logging.getLogger(__name__)

BRIDGE_BASE_URL = "http://mcp.local"
INVALID_ID_HINT = "use the id exactly as returned by Resume Matcher."


class InvalidIdentifierError(ToolError):
    """Raised when an id cannot be used as a single URL path segment."""


def path_segment(value: str, name: str = "id") -> str:
    """Return ``value`` if it is a safe single path segment.

    Raises:
        InvalidIdentifierError: If ``value`` is empty, too long, or contains
            anything other than letters, digits, ``-`` or ``_``.
    """
    try:
        return internal_client.path_segment(value, name)
    except internal_client.InvalidIdentifierError as exc:
        raise InvalidIdentifierError(f"Invalid {name}: {INVALID_ID_HINT}") from exc


class BridgeError(ToolError):
    """A non-2xx router response translated into a client-safe tool error.

    ``retry_after`` carries the router's ``Retry-After`` header (for 429s).
    """

    def __init__(self, message: str, status_code: int, retry_after: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


def upload_content_type(filename: str) -> str:
    """Return the MIME type the upload router expects for ``filename``.

    Raises:
        ToolError: If the extension is not one the upload router accepts.
    """
    suffix = Path(filename).suffix.lower()
    content_type = UPLOAD_CONTENT_TYPES.get(suffix)
    if content_type is None:
        allowed = ", ".join(sorted(UPLOAD_CONTENT_TYPES))
        raise ToolError(f"Unsupported file type '{suffix or filename}'. Allowed: {allowed}.")
    return content_type


def check_upload_size(size: int) -> None:
    """Reject files the upload router would refuse, before sending them.

    Raises:
        ToolError: If ``size`` is zero or exceeds the 4 MB upload limit.
    """
    if size <= 0:
        raise ToolError("The file is empty.")
    if size > MAX_UPLOAD_BYTES:
        raise ToolError(
            f"File is {size / (1024 * 1024):.1f} MB; the upload limit is "
            f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
        )


class AppBridge:
    """HTTP client bound to the FastAPI app through ``httpx.ASGITransport``."""

    def __init__(self, app: ASGIApp) -> None:
        # Unhandled router exceptions become generic 500s (logged here, never
        # forwarded); route budgets already bound AI calls, so no timeout.
        self._client = InternalClient(app, base_url=BRIDGE_BASE_URL, log=logger)

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        data: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
    ) -> httpx.Response:
        """Send a request to ``/api/v1{path}`` and return the 2xx response.

        Raises:
            InvalidIdentifierError: If ``path`` is not made of safe segments
                (defense in depth; tools validate ids with ``path_segment``).
            BridgeError: For any non-2xx response, carrying the router's
                client-safe detail (or a generic message for detail-less 5xx).
        """
        try:
            return await self._client.request(
                method, path, params=params, json=json, data=data, files=files
            )
        except internal_client.InvalidIdentifierError as exc:
            logger.warning("MCP bridge refused unsafe route path %r", path)
            raise InvalidIdentifierError(f"Invalid id: {INVALID_ID_HINT}") from exc
        except InternalRequestError as exc:
            raise BridgeError(str(exc), exc.status_code, exc.retry_after) from exc

    async def get_json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        """GET a route and decode its JSON body."""
        return (await self.request("GET", path, params=params)).json()

    async def post_json(self, path: str, body: Any = None) -> Any:
        """POST a JSON body and decode the JSON response."""
        return (await self.request("POST", path, json=body)).json()

    async def patch_json(self, path: str, body: Any) -> Any:
        """PATCH a JSON body and decode the JSON response."""
        return (await self.request("PATCH", path, json=body)).json()

    async def upload(
        self,
        path: str,
        filename: str,
        content: bytes,
        fields: dict[str, str] | None = None,
    ) -> Any:
        """POST a single-file multipart upload (plus form ``fields``), validated like the router.

        Raises:
            ToolError: If the extension or size would be rejected by the router.
            BridgeError: If the router rejects the upload.
        """
        content_type = upload_content_type(filename)
        check_upload_size(len(content))
        response = await self.request(
            "POST",
            path,
            data=fields,
            files={"file": (Path(filename).name, content, content_type)},
        )
        return response.json()
