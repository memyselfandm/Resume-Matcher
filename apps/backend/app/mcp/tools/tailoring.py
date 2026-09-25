"""Two-step resume tailoring: preview (cached) then confirm."""

from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from app.config import settings
from app.mcp.previews import CachedPreview, preview_expiry
from app.mcp.runtime import MCPRuntime, WaitSeconds

MAX_LISTED_CHANGES = 20
MAX_LISTED_KEYWORDS = 15
CHANGE_VALUE_CHARS = 160
PREVIEW_MISS_MESSAGE = (
    "Preview expired or unknown (previews live in this MCP server's memory); "
    "run tailor_resume_preview again."
)


def _truncate(value: Any) -> Any:
    if isinstance(value, str) and len(value) > CHANGE_VALUE_CHARS:
        return value[:CHANGE_VALUE_CHARS] + "..."
    return value


def _preview_summary(data: dict[str, Any]) -> dict[str, Any]:
    """Condense the preview response to what an agent needs to decide."""
    changes = [
        {
            "field": change.get("field_path"),
            "change": change.get("change_type"),
            "new_value": _truncate(change.get("new_value")),
        }
        for change in (data.get("detailed_changes") or [])[:MAX_LISTED_CHANGES]
    ]
    ats = data.get("ats_score") or {}
    return {
        "preview_id": data["preview_id"],
        "preview_expires_at": data.get("preview_expires_at"),
        "job_id": data.get("job_id"),
        "summary_of_changes": {
            "stats": data.get("diff_summary"),
            "changes": changes,
            "total_listed": len(changes),
        },
        "keyword_score": {
            "overall_score": ats.get("overall_score"),
            "sub_scores": ats.get("sub_scores"),
            "missing_keywords": (ats.get("missing_keywords") or [])[:MAX_LISTED_KEYWORDS],
        }
        if ats
        else None,
        "improvements": [item.get("suggestion") for item in data.get("improvements") or []],
        "warnings": data.get("warnings") or [],
    }


async def _find_application_id(runtime: MCPRuntime, job_id: str, resume_id: str) -> str | None:
    """Locate the tracker card the confirm route auto-creates."""
    board = await runtime.bridge.get_json("/applications")
    for cards in (board.get("columns") or {}).values():
        for card in cards:
            if card.get("job_id") == job_id and card.get("resume_id") == resume_id:
                return card.get("application_id")
    return None


async def request_preview(
    runtime: MCPRuntime, resume_id: str, job_id: str, prompt_id: str | None
) -> tuple[CachedPreview, dict[str, Any]]:
    """Generate a preview, cache it for confirmation and return both.

    Raises:
        ToolError: If the route fails or does not register the preview.
    """
    body: dict[str, Any] = {"resume_id": resume_id, "job_id": job_id}
    if prompt_id is not None:
        body["prompt_id"] = prompt_id
    data = (await runtime.bridge.post_json("/resumes/improve/preview", body))["data"]
    if not data.get("preview_id"):
        raise ToolError("The preview could not be registered. Please try again.")
    preview = CachedPreview(
        preview_id=data["preview_id"],
        resume_id=resume_id,
        job_id=data.get("job_id") or job_id,
        improved_data=data["resume_preview"],
        improvements=data.get("improvements") or [],
        expires_at=preview_expiry(
            data.get("preview_expires_at"),
            settings.preview_ttl_seconds,
            runtime.previews.now(),
        ),
    )
    runtime.previews.put(preview)
    return preview, data


async def confirm_preview(runtime: MCPRuntime, preview: CachedPreview) -> dict[str, Any]:
    """Save a cached preview and locate the tracker card the route created.

    The preview stays cached until it expires: a retry replays the router's
    stored confirmation instead of creating anything new.
    """
    body = await runtime.bridge.post_json(
        "/resumes/improve/confirm",
        {
            "resume_id": preview.resume_id,
            "job_id": preview.job_id,
            "preview_id": preview.preview_id,
            "improved_data": preview.improved_data,
            "improvements": preview.improvements,
        },
    )
    data = body["data"]
    tailored_resume_id = data["resume_id"]
    application_id = await _find_application_id(runtime, preview.job_id, tailored_resume_id)
    return {
        "tailored_resume_id": tailored_resume_id,
        "source_resume_id": preview.resume_id,
        "job_id": preview.job_id,
        "application_id": application_id,
        "has_cover_letter": bool(data.get("cover_letter")),
        "has_outreach_message": bool(data.get("outreach_message")),
        "warnings": data.get("warnings") or [],
    }


def register(server: MCPServer, runtime: MCPRuntime) -> None:
    """Register tailoring tools."""

    @server.tool()
    async def tailor_resume_preview(
        resume_id: Annotated[str, Field(description="Source (usually master) resume id.")],
        job_id: Annotated[str, Field(description="Job id from add_jobs.")],
        ctx: Context,
        prompt_id: Annotated[
            str | None, Field(description="Optional tailoring prompt id; defaults to the configured prompt.")
        ] = None,
        wait_seconds: WaitSeconds = None,
    ) -> dict[str, Any]:
        """Generate a tailored resume preview for a job without saving it.

        Returns a preview_id, a summary of changes and the keyword score. The
        full preview is held server-side; call tailor_resume_confirm with the
        preview_id to save it. May return status=running with a task_id.
        """

        async def operation() -> dict[str, Any]:
            _, data = await request_preview(runtime, resume_id, job_id, prompt_id)
            return _preview_summary(data)

        return await runtime.run_long_operation(
            "tailor_resume_preview", operation, wait_seconds, ctx
        )

    @server.tool()
    async def tailor_resume_confirm(
        preview_id: Annotated[str, Field(description="preview_id from tailor_resume_preview.")],
        ctx: Context,
        wait_seconds: WaitSeconds = None,
    ) -> dict[str, Any]:
        """Save a previewed tailored resume.

        Creates the tailored resume (parent_id = source resume) and its
        tracker card. Returns tailored_resume_id and application_id; update
        the card with update_application rather than creating another.
        Retrying with the same preview_id returns the same result, or the
        same running task_id while the first attempt is still in progress.
        """
        preview = runtime.previews.get(preview_id)
        if preview is None:
            raise ToolError(PREVIEW_MISS_MESSAGE)

        async def operation() -> dict[str, Any]:
            return await confirm_preview(runtime, preview)

        return await runtime.run_long_operation(
            "tailor_resume_confirm",
            operation,
            wait_seconds,
            ctx,
            idempotency_key=f"tailor_resume_confirm:{preview_id}",
        )
