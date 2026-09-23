"""Generated documents: cover letter, outreach, interview prep and PDF export."""

import base64
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from app.mcp.bridge import path_segment
from app.mcp.runtime import MCPRuntime, WaitSeconds

ResumeTemplate = Literal[
    "swiss-single",
    "swiss-two-column",
    "modern",
    "modern-two-column",
    "latex",
    "clean",
    "vivid",
]
PageSize = Literal["A4", "LETTER"]

TailoredResumeId = Annotated[
    str, Field(description="Tailored resume id (from tailor_resume_confirm).")
]


def _resolve_out_path(out_path: str) -> Path:
    """Validate a local PDF destination: .pdf suffix in an existing directory."""
    path = Path(out_path).expanduser()
    if path.suffix.lower() != ".pdf":
        raise ToolError("out_path must end with .pdf.")
    if not path.parent.is_dir():
        raise ToolError(f"Directory does not exist: {path.parent}")
    if path.is_dir():
        raise ToolError("out_path points to a directory.")
    return path


def register(server: MCPServer, runtime: MCPRuntime) -> None:
    """Register document tools."""

    @server.tool()
    async def generate_cover_letter(
        resume_id: TailoredResumeId,
        ctx: Context,
        wait_seconds: WaitSeconds = None,
    ) -> dict[str, Any]:
        """Generate and save a cover letter for a tailored resume's job."""

        segment = path_segment(resume_id, "resume_id")

        async def operation() -> dict[str, Any]:
            body = await runtime.bridge.post_json(f"/resumes/{segment}/generate-cover-letter")
            return {"resume_id": resume_id, "cover_letter": body["content"]}

        return await runtime.run_long_operation("generate_cover_letter", operation, wait_seconds, ctx)

    @server.tool()
    async def generate_outreach(
        resume_id: TailoredResumeId,
        ctx: Context,
        wait_seconds: WaitSeconds = None,
    ) -> dict[str, Any]:
        """Generate and save a recruiter outreach message for a tailored resume's job."""

        segment = path_segment(resume_id, "resume_id")

        async def operation() -> dict[str, Any]:
            body = await runtime.bridge.post_json(f"/resumes/{segment}/generate-outreach")
            return {"resume_id": resume_id, "outreach_message": body["content"]}

        return await runtime.run_long_operation("generate_outreach", operation, wait_seconds, ctx)

    @server.tool()
    async def generate_interview_prep(
        resume_id: TailoredResumeId,
        ctx: Context,
        wait_seconds: WaitSeconds = None,
    ) -> dict[str, Any]:
        """Generate and save interview preparation for a tailored resume's job."""

        segment = path_segment(resume_id, "resume_id")

        async def operation() -> dict[str, Any]:
            body = await runtime.bridge.post_json(f"/resumes/{segment}/generate-interview-prep")
            return {"resume_id": resume_id, "interview_prep": body["interview_prep"]}

        return await runtime.run_long_operation("generate_interview_prep", operation, wait_seconds, ctx)

    @server.tool()
    async def export_resume_pdf(
        resume_id: Annotated[str, Field(description="Resume id.")],
        ctx: Context,
        template: Annotated[ResumeTemplate, Field(description="Resume template.")] = "swiss-single",
        page_size: Annotated[PageSize, Field(description="Page size.")] = "A4",
        out_path: Annotated[
            str | None,
            Field(description="stdio only: write the PDF to this local .pdf path instead of returning base64."),
        ] = None,
        wait_seconds: WaitSeconds = None,
    ) -> dict[str, Any]:
        """Render a resume to PDF with the given template.

        Requires the frontend and a backend on the same data directory (see
        get_status.pdf_export_ready). On stdio, pass out_path to write a file;
        otherwise the PDF is returned as base64.
        """
        segment = path_segment(resume_id, "resume_id")
        destination: Path | None = None
        if out_path is not None:
            if not runtime.local_files_allowed:
                raise ToolError("out_path is only available on the stdio transport.")
            destination = _resolve_out_path(out_path)

        async def operation() -> dict[str, Any]:
            response = await runtime.bridge.request(
                "GET",
                f"/resumes/{segment}/pdf",
                params={"template": template, "pageSize": page_size},
            )
            pdf = response.content
            result: dict[str, Any] = {
                "resume_id": resume_id,
                "template": template,
                "page_size": page_size,
                "bytes": len(pdf),
            }
            if destination is not None:
                destination.write_bytes(pdf)
                result["path"] = str(destination)
            else:
                result["filename"] = f"resume_{resume_id}.pdf"
                result["content_base64"] = base64.b64encode(pdf).decode("ascii")
            return result

        return await runtime.run_long_operation("export_resume_pdf", operation, wait_seconds, ctx)
