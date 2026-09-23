"""Per-server state shared by MCP tools: bridge, caches and wait policy."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from fastapi import FastAPI
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from app.config import settings
from app.mcp.bridge import AppBridge
from app.mcp.previews import PreviewCache
from app.mcp.tasks import TaskRegistry

logger = logging.getLogger(__name__)

Transport = Literal["stdio", "http"]

# stdio clients (Claude Code/Desktop) tolerate long calls; progress heartbeats
# keep them informed. HTTP JSON responses cannot stream progress, and typical
# client request timeouts are ~60 s, so HTTP waits less and caps below the
# proxy timeout.
STDIO_DEFAULT_WAIT_SECONDS = 200.0
STDIO_MAX_WAIT_SECONDS = 1800.0
HTTP_DEFAULT_WAIT_SECONDS = 45.0
HTTP_PROXY_MARGIN_SECONDS = 20.0
HEARTBEAT_INTERVAL_SECONDS = 15.0

WaitSeconds = Annotated[
    float | None,
    Field(
        description=(
            "Seconds to wait for completion before returning a task_id to poll "
            "with get_task. Defaults: 200 (stdio), 45 (HTTP)."
        ),
        ge=0,
    ),
]


@dataclass
class MCPRuntime:
    """Everything a tool needs beyond its arguments."""

    bridge: AppBridge
    transport: Transport
    previews: PreviewCache = field(default_factory=PreviewCache)
    tasks: TaskRegistry = field(default_factory=TaskRegistry)
    heartbeat_interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS

    @classmethod
    def create(cls, app: FastAPI, transport: Transport) -> "MCPRuntime":
        """Build a runtime whose bridge targets ``app`` in-process."""
        return cls(bridge=AppBridge(app), transport=transport)

    async def aclose(self) -> None:
        """Cancel outstanding tasks and close the bridge."""
        try:
            await self.tasks.shutdown()
        finally:
            await self.bridge.aclose()

    @property
    def local_files_allowed(self) -> bool:
        """Whether tools may read or write local paths (stdio only)."""
        return self.transport == "stdio"

    def max_wait_seconds(self) -> float:
        """Upper bound for ``wait_seconds`` on this transport."""
        if self.transport == "http":
            return max(settings.request_timeout_seconds - HTTP_PROXY_MARGIN_SECONDS, 1.0)
        return STDIO_MAX_WAIT_SECONDS

    def resolve_wait_seconds(self, requested: float | None) -> float:
        """Apply the transport default and clamp to ``[0, max_wait_seconds]``."""
        if requested is None:
            default = (
                HTTP_DEFAULT_WAIT_SECONDS
                if self.transport == "http"
                else STDIO_DEFAULT_WAIT_SECONDS
            )
            requested = default
        return min(max(float(requested), 0.0), self.max_wait_seconds())

    async def run_long_operation(
        self,
        name: str,
        operation: Callable[[], Awaitable[dict[str, Any]]],
        wait_seconds: float | None,
        ctx: Context | None = None,
    ) -> dict[str, Any]:
        """Run ``operation`` as a task and wait for it within the wait budget.

        Returns the operation's result (plus ``status``/``task_id``) when it
        finishes in time, otherwise ``{"status": "running", "task_id": ...}``
        for polling with ``get_task``.

        Raises:
            ToolError: If the operation fails or is cancelled within the wait.
        """
        record = self.tasks.start(name, operation)
        budget = self.resolve_wait_seconds(wait_seconds)
        loop = asyncio.get_running_loop()
        started = loop.time()
        while True:
            remaining = budget - (loop.time() - started)
            step = min(remaining, self.heartbeat_interval_seconds)
            current = await self.tasks.wait(record.task_id, step)
            if current is None or current.status != "running":
                break
            if loop.time() - started >= budget:
                break
            await _heartbeat(ctx, loop.time() - started, name)

        if record.status == "succeeded":
            return {"status": "succeeded", "task_id": record.task_id, **(record.result or {})}
        if record.status in ("failed", "cancelled"):
            raise ToolError(record.error or "The operation did not complete.")
        return {
            "status": "running",
            "task_id": record.task_id,
            "message": f"{name} is still running; poll get_task with this task_id.",
        }


async def _heartbeat(ctx: Context | None, elapsed: float, name: str) -> None:
    """Send a best-effort progress notification while an operation runs."""
    if ctx is None:
        return
    try:
        await ctx.report_progress(elapsed, None, f"{name} still running")
    except Exception:  # noqa: BLE001 - progress is advisory; never fail the tool on it
        logger.debug("MCP progress heartbeat failed", exc_info=True)
