"""Read-only MCP resources: ``resume://{resume_id}`` and ``job://{job_id}``."""

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ResourceNotFoundError

from app.mcp.bridge import BridgeError
from app.mcp.formatting import resume_markdown
from app.mcp.runtime import MCPRuntime
from app.mcp.tools.resumes import fetch_resume


def _resource_error(error: BridgeError, kind: str) -> ResourceError:
    """Map a bridge failure to a protocol error (404 -> -32602)."""
    if error.status_code == 404:
        return ResourceNotFoundError(f"{kind} not found")
    return ResourceError(str(error))


def register(server: MCPServer, runtime: MCPRuntime) -> None:
    """Register resume and job resource templates."""

    @server.resource(
        "resume://{resume_id}",
        name="resume",
        description="A stored resume rendered as Markdown.",
        mime_type="text/markdown",
    )
    async def resume_resource(resume_id: str) -> str:
        try:
            data = await fetch_resume(runtime, resume_id)
        except BridgeError as error:
            raise _resource_error(error, "Resume") from None
        return resume_markdown(data)

    @server.resource(
        "job://{job_id}",
        name="job",
        description="A stored job description as plain text.",
        mime_type="text/plain",
    )
    async def job_resource(job_id: str) -> str:
        try:
            job = await runtime.bridge.get_json(f"/jobs/{job_id}")
        except BridgeError as error:
            raise _resource_error(error, "Job") from None
        return str(job.get("content") or "")
