"""In-process ASGI bridge from MCP tools to the FastAPI routers.

Tools call the same HTTP routes the web UI uses, so validation, time budgets,
error translation and side effects (tracker auto-creation, preview claims)
stay identical without touching router code. Requests never leave the process.
"""

import logging
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from mcp.server.mcpserver.exceptions import ToolError

logger = logging.getLogger(__name__)

BRIDGE_BASE_URL = "http://mcp.local"
API_PREFIX = "/api/v1"

# Mirrors the upload router's accepted extensions and raw size limit
# (app/routers/resumes.py DOCUMENT_TYPES_BY_EXTENSION / MAX_FILE_SIZE).
UPLOAD_CONTENT_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
MAX_UPLOAD_BYTES = 4 * 1024 * 1024

GENERIC_SERVER_ERROR = "Resume Matcher failed to complete the request. Please try again."


class BridgeError(ToolError):
    """A non-2xx router response translated into a client-safe tool error."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


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


def _error_message(response: httpx.Response) -> str:
    """Extract the router's client-safe ``detail`` without leaking internals."""
    detail: Any = None
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        detail = body.get("detail")

    if isinstance(detail, str) and detail.strip():
        return detail
    if isinstance(detail, list):
        # FastAPI request-validation errors: report field locations and messages only.
        parts: list[str] = []
        for item in detail:
            if not isinstance(item, dict):
                continue
            location = ".".join(str(part) for part in item.get("loc", ()) if part != "body")
            message = item.get("msg", "invalid value")
            parts.append(f"{location}: {message}" if location else str(message))
        if parts:
            return "Invalid request: " + "; ".join(parts)
    if response.status_code >= 500:
        return GENERIC_SERVER_ERROR
    return f"Request failed with status {response.status_code}."


class AppBridge:
    """HTTP client bound to the FastAPI app through ``httpx.ASGITransport``."""

    def __init__(self, app: FastAPI) -> None:
        # raise_app_exceptions=False turns unhandled router exceptions into 500
        # responses instead of re-raising tracebacks into the tool layer. Route
        # budgets already bound AI calls, so the client applies no timeout.
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url=BRIDGE_BASE_URL,
            timeout=None,
        )

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
        files: dict[str, tuple[str, bytes, str]] | None = None,
    ) -> httpx.Response:
        """Send a request to ``/api/v1{path}`` and return the 2xx response.

        Raises:
            BridgeError: For any non-2xx response, carrying the router's
                client-safe detail (or a generic message for detail-less 5xx).
        """
        response = await self._client.request(
            method,
            f"{API_PREFIX}{path}",
            params=params,
            json=json,
            files=files,
        )
        if response.is_success:
            return response
        message = _error_message(response)
        if response.status_code >= 500:
            logger.warning(
                "MCP bridge %s %s returned %s", method, path, response.status_code
            )
        raise BridgeError(message, response.status_code)

    async def get_json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        """GET a route and decode its JSON body."""
        return (await self.request("GET", path, params=params)).json()

    async def post_json(self, path: str, body: Any = None) -> Any:
        """POST a JSON body and decode the JSON response."""
        return (await self.request("POST", path, json=body)).json()

    async def patch_json(self, path: str, body: Any) -> Any:
        """PATCH a JSON body and decode the JSON response."""
        return (await self.request("PATCH", path, json=body)).json()

    async def upload(self, path: str, filename: str, content: bytes) -> Any:
        """POST a single-file multipart upload, validated like the router.

        Raises:
            ToolError: If the extension or size would be rejected by the router.
            BridgeError: If the router rejects the upload.
        """
        content_type = upload_content_type(filename)
        check_upload_size(len(content))
        response = await self.request(
            "POST",
            path,
            files={"file": (Path(filename).name, content, content_type)},
        )
        return response.json()
