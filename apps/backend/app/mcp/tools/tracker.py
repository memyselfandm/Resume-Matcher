"""Application-tracker (Kanban) tools."""

from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field

from app.mcp.bridge import path_segment
from app.mcp.runtime import MCPRuntime
from app.schemas.applications import ApplicationStatus


def register(server: MCPServer, runtime: MCPRuntime) -> None:
    """Register tracker tools."""

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def list_applications(
        status: Annotated[
            ApplicationStatus | None, Field(description="Only return cards in this column.")
        ] = None,
    ) -> dict[str, Any]:
        """List tracker cards grouped by status column."""
        board = await runtime.bridge.get_json("/applications")
        columns: dict[str, list[dict[str, Any]]] = board.get("columns") or {}
        if status is not None:
            columns = {status.value: columns.get(status.value, [])}
        return {
            "columns": columns,
            "total": sum(len(cards) for cards in columns.values()),
        }

    @server.tool()
    async def create_application(
        resume_id: Annotated[str, Field(description="Resume used for this application.")],
        job_description: Annotated[str, Field(description="Full job description text.", min_length=1)],
        company: Annotated[str | None, Field(description="Company (extracted when omitted).")] = None,
        role: Annotated[str | None, Field(description="Role (extracted when omitted).")] = None,
        status: Annotated[ApplicationStatus, Field(description="Initial column.")] = ApplicationStatus.applied,
        notes: Annotated[str | None, Field(description="Free-form notes.")] = None,
    ) -> dict[str, Any]:
        """Add a tracker card for an application made without tailoring.

        tailor_resume_confirm already creates a card; do not call this for
        tailored resumes.
        """
        return await runtime.bridge.post_json(
            "/applications",
            {
                "resume_id": resume_id,
                "job_description": job_description,
                "company": company,
                "role": role,
                "status": status.value,
                "notes": notes,
            },
        )

    @server.tool(annotations=ToolAnnotations(idempotent_hint=True))
    async def update_application(
        application_id: Annotated[str, Field(description="Tracker card id.")],
        status: Annotated[ApplicationStatus | None, Field(description="Move to this column.")] = None,
        notes: Annotated[str | None, Field(description="Replace the card's notes.")] = None,
        company: Annotated[str | None, Field(description="Company name.")] = None,
        role: Annotated[str | None, Field(description="Role title.")] = None,
        applied_at: Annotated[str | None, Field(description="ISO-8601 date the application was sent.")] = None,
        position: Annotated[int | None, Field(description="Position within the column.")] = None,
    ) -> dict[str, Any]:
        """Update a tracker card; only the fields provided are changed."""
        updates: dict[str, Any] = {
            key: value
            for key, value in {
                "notes": notes,
                "company": company,
                "role": role,
                "applied_at": applied_at,
                "position": position,
            }.items()
            if value is not None
        }
        if status is not None:
            updates["status"] = status.value
        segment = path_segment(application_id, "application_id")
        return await runtime.bridge.patch_json(f"/applications/{segment}", updates)
