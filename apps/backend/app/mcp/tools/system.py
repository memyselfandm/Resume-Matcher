"""Status and task-polling tools."""

import asyncio
import logging
import os
from typing import Annotated, Any, Literal

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from app.config import settings
from app.database import db
from app.instance_id import get_db_instance_id
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


async def _probe_backend(origin: str) -> tuple[bool, str | None]:
    """Return (reachable, db_instance_id) for the backend at ``origin``."""
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
            response = await client.get(f"{origin}/api/v1/health")
    except Exception:  # noqa: BLE001 - any failure means "not reachable"
        logger.debug("MCP status: backend probe failed for %s", origin, exc_info=True)
        return False, None
    if not response.is_success:
        return False, None
    try:
        body = response.json()
    except ValueError:
        return True, None
    instance_id = body.get("db_instance_id") if isinstance(body, dict) else None
    return True, instance_id if isinstance(instance_id, str) else None


async def _probe_frontend(url: str) -> bool:
    """Return whether the frontend answers at ``url``."""
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
            response = await client.get(url)
    except Exception:  # noqa: BLE001 - any failure means "not reachable"
        logger.debug("MCP status: frontend probe failed for %s", url, exc_info=True)
        return False
    return response.status_code < 500


def _render_path(
    backend_reachable: bool,
    remote_id: str | None,
    local_id: str | None,
    local_resume_count: int,
    origin: str,
) -> tuple[RenderPathOk, str]:
    """Decide whether the print page reads the same database as this process."""
    if not backend_reachable:
        return False, (
            f"No backend answered at {origin}. PDF export needs the backend HTTP "
            "server running on the same DATA_DIR as this MCP server."
        )
    if remote_id is None:
        return "unknown", f"The backend at {origin} does not report db_instance_id."
    if local_id is None:
        return "unknown", "This server's database instance id is unavailable."
    if local_resume_count == 0:
        return "unknown", "No resumes are stored yet, so there is nothing to render."
    if remote_id == local_id:
        return True, f"The backend at {origin} uses the same database."
    return False, (
        f"The backend at {origin} uses a different data directory; PDFs would "
        "render another database's resumes. Point both at the same DATA_DIR."
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
        determined (for example, no resumes stored yet).
        """
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
        (backend_reachable, remote_id), frontend_reachable = await asyncio.gather(
            _probe_backend(origin), _probe_frontend(frontend_url)
        )
        local_id: str | None = None
        try:
            local_id = get_db_instance_id()
        except OSError:
            logger.exception("MCP status: database instance id unavailable")
        render_path_ok, render_path_detail = _render_path(
            backend_reachable,
            remote_id,
            local_id,
            int(stats.get("total_resumes", 0)),
            origin,
        )
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
        included) or cancelled. Finished tasks are kept for one hour.
        """
        record = runtime.tasks.get(task_id)
        if record is None:
            raise ToolError("Unknown or expired task_id.")
        return record.to_dict()

    @server.tool()
    async def cancel_task(
        task_id: Annotated[str, Field(description="task_id of a running operation.")],
    ) -> dict[str, Any]:
        """Cancel a running operation. Work already committed is not rolled back."""
        record = runtime.tasks.cancel(task_id)
        if record is None:
            raise ToolError("Unknown or expired task_id.")
        settled = await runtime.tasks.wait(task_id, 1.0)
        return (settled or record).to_dict()
