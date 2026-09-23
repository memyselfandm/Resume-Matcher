"""Status and task-polling tools."""

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Annotated, Any, Literal

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from app.config import settings
from app.database import db
from app.instance_id import database_established, get_db_instance_id
from app.llm import get_llm_config
from app.mcp.runtime import MCPRuntime

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_SECONDS = 3.0
# Where the Next.js print page fetches resume data server-side
# (apps/frontend/lib/api/client.ts: NEXT_PUBLIC_API_URL when absolute,
# otherwise the internal backend origin).
INTERNAL_API_ORIGIN = "http://127.0.0.1:8000"

RenderPathOk = bool | Literal["unknown"]


def print_data_origin() -> str:
    """Return the backend origin the print page reads resume data from."""
    configured = os.environ.get("NEXT_PUBLIC_API_URL", "").strip().rstrip("/")
    if configured.startswith(("http://", "https://")):
        return configured
    return INTERNAL_API_ORIGIN


@dataclass(frozen=True)
class BackendProbe:
    """What the backend at the print page's data origin reported."""

    reachable: bool
    reports_instance_id: bool = False
    instance_id: str | None = None


async def _probe_backend(origin: str) -> BackendProbe:
    """Ask the backend at ``origin`` for its ``db_instance_id``."""
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
            response = await client.get(f"{origin}/api/v1/health")
    except Exception:  # noqa: BLE001 - any failure means "not reachable"
        logger.debug("MCP status: backend probe failed for %s", origin, exc_info=True)
        return BackendProbe(reachable=False)
    if not response.is_success:
        return BackendProbe(reachable=False)
    try:
        body = response.json()
    except ValueError:
        return BackendProbe(reachable=True)
    if not isinstance(body, dict) or "db_instance_id" not in body:
        return BackendProbe(reachable=True)
    instance_id = body["db_instance_id"]
    return BackendProbe(
        reachable=True,
        reports_instance_id=True,
        instance_id=instance_id if isinstance(instance_id, str) else None,
    )


async def _probe_frontend(url: str) -> bool:
    """Return whether the frontend answers at ``url``."""
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
            response = await client.get(url)
    except Exception:  # noqa: BLE001 - any failure means "not reachable"
        logger.debug("MCP status: frontend probe failed for %s", url, exc_info=True)
        return False
    return response.status_code < 500


def _render_path(probe: BackendProbe, local_id: str | None, origin: str) -> tuple[RenderPathOk, str]:
    """Decide whether the print page reads the same database as this process.

    Ids are compared whenever both sides have one. A side without an id has
    no database yet, so exactly one missing id means different directories;
    both missing (two empty data directories) cannot be decided.
    """
    if not probe.reachable:
        return False, (
            f"No backend answered at {origin}. PDF export needs the backend HTTP "
            "server running on the same DATA_DIR as this MCP server."
        )
    if not probe.reports_instance_id:
        return "unknown", f"The backend at {origin} does not report db_instance_id."
    remote_id = probe.instance_id
    if remote_id is not None and local_id is not None:
        if remote_id == local_id:
            return True, f"The backend at {origin} uses the same database."
        return False, (
            f"The backend at {origin} uses a different data directory; PDFs would "
            "render another database's resumes. Point both at the same DATA_DIR."
        )
    if remote_id is None and local_id is None:
        return "unknown", "Neither database has been created yet, so identity cannot be established."
    return False, (
        f"Only one of this server and the backend at {origin} has a database, so "
        "they use different data directories. Point both at the same DATA_DIR."
    )


def register(server: MCPServer, runtime: MCPRuntime) -> None:
    """Register the status tool."""

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def get_status() -> dict[str, Any]:
        """Report readiness without calling the LLM provider.

        Returns whether an LLM is configured, database counts, whether the
        frontend is reachable, and render_path_ok: true when the backend that
        serves the print page uses this server's database, false when it is
        unreachable or uses another database, "unknown" when it cannot be
        determined (neither database created yet, or an older backend).
        """
        # Checked first: reading the LLM config or stats creates the database.
        local_established = False
        try:
            local_established = database_established()
        except OSError:
            logger.exception("MCP status: data directory unavailable")

        llm_configured = False
        llm_provider: str | None = None
        llm_model: str | None = None
        try:
            config = get_llm_config()
            llm_provider = config.provider
            llm_model = config.model
            # ollama / openai_compatible run without a key (matches GET /status).
            llm_configured = bool(config.api_key) or config.provider in (
                "ollama",
                "openai_compatible",
            )
        except Exception:  # noqa: BLE001 - degrade this field only
            logger.exception("MCP status: LLM configuration unavailable")

        stats: dict[str, Any] = {}
        try:
            stats = await db.get_stats()
        except Exception:  # noqa: BLE001 - degrade this field only
            logger.exception("MCP status: database stats failed")

        origin = print_data_origin()
        frontend_url = settings.frontend_base_url.rstrip("/")
        probe, frontend_reachable = await asyncio.gather(
            _probe_backend(origin), _probe_frontend(frontend_url)
        )
        render_path_ok: RenderPathOk
        local_id: str | None = None
        try:
            local_id = get_db_instance_id(create=local_established)
            render_path_ok, render_path_detail = _render_path(probe, local_id, origin)
        except OSError:
            logger.exception("MCP status: database instance id unavailable")
            render_path_ok = "unknown"
            render_path_detail = "This server's database instance id is unavailable."
        return {
            "llm_configured": llm_configured,
            "llm_provider": llm_provider,
            "llm_model": llm_model,
            "database": stats,
            "db_instance_id": local_id,
            "frontend": {"url": frontend_url, "reachable": frontend_reachable},
            "print_data_origin": origin,
            "render_path_ok": render_path_ok,
            "render_path_detail": render_path_detail,
            "pdf_export_ready": frontend_reachable and render_path_ok is True,
            "transport": runtime.transport,
        }


def register_task_tools(server: MCPServer, runtime: MCPRuntime) -> None:
    """Register task polling and cancellation tools."""

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def get_task(
        task_id: Annotated[str, Field(description="task_id returned by a long-running tool.")],
    ) -> dict[str, Any]:
        """Poll a long-running operation started by another tool.

        status is running, succeeded (result included), failed (error
        included) or cancelled. Finished tasks are kept for one hour. Large
        payloads (a PDF's content_base64) are returned once and then released.
        """
        record = runtime.tasks.get(task_id)
        if record is None:
            raise ToolError("Unknown or expired task_id.")
        payload = record.to_dict()
        record.mark_delivered()
        return payload

    @server.tool()
    async def cancel_task(
        task_id: Annotated[str, Field(description="task_id of a running operation.")],
    ) -> dict[str, Any]:
        """Cancel a running operation. Work already committed is not rolled back."""
        record = runtime.tasks.cancel(task_id)
        if record is None:
            raise ToolError("Unknown or expired task_id.")
        settled = await runtime.tasks.wait(task_id, 1.0) or record
        payload = settled.to_dict()
        settled.mark_delivered()
        return payload
