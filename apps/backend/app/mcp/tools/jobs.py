"""Job description tools."""

from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations
from pydantic import Field

from app.mcp.bridge import path_segment
from app.mcp.runtime import MCPRuntime

JOB_PREVIEW_CHARS = 500


def register(server: MCPServer, runtime: MCPRuntime) -> None:
    """Register job tools."""

    @server.tool()
    async def add_jobs(
        descriptions: Annotated[
            list[str], Field(description="One or more full job description texts.", min_length=1)
        ],
        resume_id: Annotated[
            str | None, Field(description="Optional resume id to associate with the jobs.")
        ] = None,
    ) -> dict[str, Any]:
        """Store job descriptions for tailoring; returns one job_id per description."""
        body = await runtime.bridge.post_json(
            "/jobs/upload",
            {"job_descriptions": descriptions, "resume_id": resume_id},
        )
        return {"job_ids": body["job_id"]}

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def get_job(
        job_id: Annotated[str, Field(description="Job id.")],
        full: Annotated[
            bool, Field(description="Return the full description instead of a preview.")
        ] = False,
    ) -> dict[str, Any]:
        """Fetch a stored job description and its extracted company/role."""
        job = await runtime.bridge.get_json(f"/jobs/{path_segment(job_id, 'job_id')}")
        content = job.get("content") or ""
        result: dict[str, Any] = {
            "job_id": job.get("job_id", job_id),
            "company": job.get("company"),
            "role": job.get("role"),
            "resume_id": job.get("resume_id"),
            "created_at": job.get("created_at"),
            "content_length": len(content),
        }
        if full or len(content) <= JOB_PREVIEW_CHARS:
            result["content"] = content
        else:
            result["content_preview"] = content[:JOB_PREVIEW_CHARS]
        return result
