"""Resume upload, retrieval and editing tools."""

import base64
import binascii
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from app.mcp.bridge import MAX_UPLOAD_BYTES, check_upload_size, upload_content_type
from app.mcp.formatting import resume_markdown, resume_summary
from app.mcp.runtime import MCPRuntime

ResumeFormat = Literal["summary", "json", "markdown"]


def _read_local_file(path: str) -> tuple[str, bytes]:
    """Read an upload from disk after checking its type and size."""
    file_path = Path(path).expanduser()
    if not file_path.is_file():
        raise ToolError(f"File not found: {file_path}")
    upload_content_type(file_path.name)
    check_upload_size(file_path.stat().st_size)
    return file_path.name, file_path.read_bytes()


def _decode_base64(content_base64: str) -> bytes:
    """Decode an upload payload, refusing oversized input before decoding."""
    # Every 4 base64 characters carry 3 bytes; reject early without decoding.
    if len(content_base64) * 3 // 4 > MAX_UPLOAD_BYTES + 3:
        check_upload_size(len(content_base64) * 3 // 4)
    try:
        return base64.b64decode(content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ToolError("content_base64 is not valid base64.") from exc


async def fetch_resume(runtime: MCPRuntime, resume_id: str) -> dict[str, Any]:
    """Return the ``data`` payload of ``GET /resumes?resume_id=``."""
    body = await runtime.bridge.get_json("/resumes", params={"resume_id": resume_id})
    return body["data"]


def register(server: MCPServer, runtime: MCPRuntime) -> None:
    """Register resume tools."""

    @server.tool()
    async def upload_resume(
        filename: Annotated[
            str | None,
            Field(description="File name with .pdf, .doc or .docx extension (required with content_base64)."),
        ] = None,
        content_base64: Annotated[
            str | None, Field(description="Base64-encoded file content (max 4 MB decoded).")
        ] = None,
        path: Annotated[
            str | None,
            Field(description="Local file path to upload. Only available on the stdio transport."),
        ] = None,
    ) -> dict[str, Any]:
        """Upload a PDF, DOC or DOCX resume and parse it into structured data.

        Provide either path (stdio only) or filename + content_base64. The
        first uploaded resume becomes the master resume automatically.
        """
        if (path is None) == (content_base64 is None):
            raise ToolError("Provide exactly one of path or content_base64.")
        if path is not None:
            if not runtime.local_files_allowed:
                raise ToolError("path uploads are only available on the stdio transport; use content_base64.")
            name, content = _read_local_file(path)
        else:
            if not filename:
                raise ToolError("filename is required with content_base64.")
            upload_content_type(filename)
            name, content = filename, _decode_base64(content_base64 or "")
        body = await runtime.bridge.upload("/resumes/upload", name, content)
        return {
            "resume_id": body["resume_id"],
            "is_master": body.get("is_master", False),
            "processing_status": body.get("processing_status"),
            "message": body.get("message"),
        }

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def list_resumes(
        include_master: Annotated[bool, Field(description="Include the master resume.")] = True,
    ) -> dict[str, Any]:
        """List stored resumes, most recently updated first."""
        body = await runtime.bridge.get_json(
            "/resumes/list", params={"include_master": str(include_master).lower()}
        )
        return {"resumes": body["data"]}

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def get_resume(
        resume_id: Annotated[str, Field(description="Resume id.")],
        format: Annotated[
            ResumeFormat,
            Field(description="summary (default), json (full ResumeData) or markdown."),
        ] = "summary",
    ) -> dict[str, Any]:
        """Fetch a resume as a compact summary, full structured JSON or Markdown."""
        data = await fetch_resume(runtime, resume_id)
        if format == "markdown":
            return {"resume_id": resume_id, "markdown": resume_markdown(data)}
        if format == "json":
            return {
                "resume_id": resume_id,
                "title": data.get("title"),
                "parent_id": data.get("parent_id"),
                "processing_status": (data.get("raw_resume") or {}).get("processing_status"),
                "resume_data": data.get("processed_resume"),
                "cover_letter": data.get("cover_letter"),
                "outreach_message": data.get("outreach_message"),
                "interview_prep": data.get("interview_prep"),
            }
        return resume_summary(data)

    @server.tool(annotations=ToolAnnotations(idempotent_hint=True))
    async def update_resume(
        resume_id: Annotated[str, Field(description="Resume id.")],
        resume_data: Annotated[
            dict[str, Any],
            Field(description="Complete ResumeData object (get_resume format=json returns one); replaces the stored data."),
        ],
    ) -> dict[str, Any]:
        """Replace a resume's structured data with a full ResumeData object."""
        body = await runtime.bridge.patch_json(f"/resumes/{resume_id}", resume_data)
        return resume_summary(body["data"])

    @server.tool(annotations=ToolAnnotations(idempotent_hint=True))
    async def set_resume_title(
        resume_id: Annotated[str, Field(description="Resume id.")],
        title: Annotated[str, Field(description="New title (trimmed to 80 characters).")],
    ) -> dict[str, Any]:
        """Set a resume's display title."""
        await runtime.bridge.patch_json(f"/resumes/{resume_id}/title", {"title": title})
        return {"resume_id": resume_id, "title": title.strip()[:80]}
