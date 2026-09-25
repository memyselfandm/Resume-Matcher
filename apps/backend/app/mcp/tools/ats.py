"""ATS parse-check tools and the tailor-then-verify workflow.

Every tool goes through the parse-check routes the web UI uses. Output is
summary first: a verdict plus the failing checks rendered in English
(``messages_en``); ``detail=true`` adds the full report(s).
"""

import asyncio
import hashlib
import json
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from app.mcp.bridge import BridgeError, path_segment, upload_content_type
from app.mcp.runtime import MCPRuntime, WaitSeconds
from app.mcp.tools.resumes import decode_base64, read_local_file
from app.mcp.tools.tailoring import confirm_preview, request_preview
from app.routers.parse_check import ContentLanguage
from app.services.ats_parse.messages_en import render_message
from app.services.ats_parse.own_output import OwnOutputParseCheck, TemplateSettings
from app.services.ats_parse.report import ParseCheckReport
from app.services.ats_parse.templates import RenderLocale, TemplateId

DEFAULT_MIN_CONTENT_RECALL = 0.95
# A failed check at these severities fails the verdict. The engine already
# weighs multi_column/sidebar as medium for two-column-by-design templates.
BLOCKING_SEVERITIES = frozenset({"fatal", "high"})
# tailor_and_verify retries a parse check refused as busy (429) this many
# times, waiting the route's Retry-After (capped) between attempts.
PARSE_CHECK_BUSY_ATTEMPTS = 3
PARSE_CHECK_BUSY_MAX_WAIT_SECONDS = 30.0
DEFAULT_RETRY_AFTER_SECONDS = 10.0

ContentLanguageArg = Annotated[
    ContentLanguage | None,
    Field(description="Language the resume is written in; detected (file) or the configured content language (stored resume) when omitted."),
]
SettingsArg = Annotated[
    TemplateSettings | None,
    Field(description="Template settings as in the web UI (template, pageSize, margins, spacing, fontSize, ...); defaults to swiss-single."),
]
DetailArg = Annotated[
    bool, Field(description="Also return the full parse-check report(s); default is a compact summary.")
]


def failing_checks(report: ParseCheckReport) -> list[dict[str, Any]]:
    """Failed checks with their severity and an English explanation."""
    items: list[dict[str, Any]] = []
    for check in report.checks:
        if check.status != "fail":
            continue
        item: dict[str, Any] = {
            "id": check.id,
            "severity": check.severity,
            "message": render_message(check),
        }
        if check.params.get("expected_by_template") is True:
            item["expected_by_template"] = True
        items.append(item)
    return items


def verdict(report: ParseCheckReport, min_content_recall: float) -> tuple[bool, list[str]]:
    """Apply the pass rule and return ``(passes, reasons it does not pass)``.

    Passes when content recall (if a round trip was computed) reaches
    ``min_content_recall`` and no failed check has fatal or high severity.
    """
    reasons: list[str] = []
    roundtrip = report.roundtrip
    if roundtrip is not None and roundtrip.content_recall < min_content_recall:
        reasons.append(
            f"content_recall {roundtrip.content_recall:g} is below {min_content_recall:g}"
        )
    blocking = [
        check.id
        for check in report.checks
        if check.status == "fail" and check.severity in BLOCKING_SEVERITIES
    ]
    if blocking:
        reasons.append("fatal/high checks failed: " + ", ".join(blocking))
    return not reasons, reasons


def report_summary(report: ParseCheckReport, min_content_recall: float) -> dict[str, Any]:
    """Compact verdict of one report: scores, recall and failing checks."""
    passes, reasons = verdict(report, min_content_recall)
    summary: dict[str, Any] = {
        "passes": passes,
        "parseability_score": report.overall_score,
        "content_score": report.content_score,
    }
    if report.roundtrip is not None:
        summary["content_recall"] = report.roundtrip.content_recall
        summary["order_fidelity"] = report.roundtrip.order_fidelity
    summary["failing_checks"] = failing_checks(report)
    if reasons:
        summary["reasons"] = reasons
    return summary


def own_output_summary(check: OwnOutputParseCheck) -> list[dict[str, Any]]:
    """Per-template summaries; templates that could not be checked say why."""
    results: list[dict[str, Any]] = []
    for result in check.results:
        entry: dict[str, Any] = {"template": result.template, "status": result.status}
        if result.expected_by_template:
            entry["two_column_by_design"] = True
        if result.report is None:
            entry["error"] = result.error
        else:
            entry.update(report_summary(result.report, DEFAULT_MIN_CONTENT_RECALL))
        results.append(entry)
    return results


def _retry_after_seconds(error: BridgeError) -> float:
    try:
        return float(error.retry_after) if error.retry_after else DEFAULT_RETRY_AFTER_SECONDS
    except ValueError:
        return DEFAULT_RETRY_AFTER_SECONDS


def _busy_error(error: BridgeError) -> ToolError:
    """A 429 from the own-output route, with a retry hint."""
    return ToolError(f"{error} Retry in {_retry_after_seconds(error):g} seconds.")


async def _post_own_output_check(
    runtime: MCPRuntime, resume_id: str, body: dict[str, Any]
) -> OwnOutputParseCheck:
    """POST the own-output parse check and validate the response."""
    segment = path_segment(resume_id, "resume_id")
    data = await runtime.bridge.post_json(f"/resumes/{segment}/parse-check", body)
    return OwnOutputParseCheck.model_validate(data)


async def _check_own_output_retrying(
    runtime: MCPRuntime, resume_id: str, body: dict[str, Any]
) -> OwnOutputParseCheck:
    """Run the own-output check, waiting out a busy renderer a few times."""
    attempt = 1
    while True:
        try:
            return await _post_own_output_check(runtime, resume_id, body)
        except BridgeError as exc:
            if exc.status_code != 429:
                raise
            if attempt >= PARSE_CHECK_BUSY_ATTEMPTS:
                raise _busy_error(exc) from exc
            delay = min(_retry_after_seconds(exc), PARSE_CHECK_BUSY_MAX_WAIT_SECONDS)
        attempt += 1
        await asyncio.sleep(delay)


def _template_settings(
    settings: TemplateSettings | None,
    template: TemplateId | None = None,
    render_locale: RenderLocale | None = None,
) -> TemplateSettings:
    """Merge the ``template``/``render_locale`` shortcuts into the settings."""
    merged = settings.model_copy(deep=True) if settings is not None else TemplateSettings()
    if template is not None:
        merged.template = template
    if render_locale is not None:
        merged.lang = render_locale
    return merged


def register(server: MCPServer, runtime: MCPRuntime) -> None:
    """Register ATS parse-check tools."""

    @server.tool()
    async def ats_parse_check_file(
        ctx: Context,
        filename: Annotated[
            str | None,
            Field(description="File name with .pdf, .doc or .docx extension (required with content_base64)."),
        ] = None,
        content_base64: Annotated[
            str | None, Field(description="Base64-encoded file content (max 4 MB decoded).")
        ] = None,
        path: Annotated[
            str | None,
            Field(description="Local file path to check. Only available on the stdio transport."),
        ] = None,
        content_language: ContentLanguageArg = None,
        detail: DetailArg = False,
        wait_seconds: WaitSeconds = None,
    ) -> dict[str, Any]:
        """Check whether an ATS-style text extractor can read a resume file.

        Provide either path (stdio only) or filename + content_base64. Nothing
        is stored. Returns passes (no fatal/high check failed), the
        parseability and content scores, and the failing checks explained in
        English; detail=true adds the full report. Deterministic, no LLM.
        """
        if (path is None) == (content_base64 is None):
            raise ToolError("Provide exactly one of path or content_base64.")
        if path is not None:
            if not runtime.local_files_allowed:
                raise ToolError("path is only available on the stdio transport; use content_base64.")
            name, content = read_local_file(path)
        else:
            if not filename:
                raise ToolError("filename is required with content_base64.")
            upload_content_type(filename)
            name, content = filename, decode_base64(content_base64 or "")
        fields = {"content_language": content_language} if content_language else None

        async def operation() -> dict[str, Any]:
            data = await runtime.bridge.upload("/ats/parse-check", name, content, fields)
            report = ParseCheckReport.model_validate(data)
            result: dict[str, Any] = {
                "file_format": report.file_format,
                "extractability": report.extractability,
                "content_language": report.content_language,
                **report_summary(report, DEFAULT_MIN_CONTENT_RECALL),
            }
            if detail:
                result["report"] = report.model_dump(mode="json")
            return result

        return await runtime.run_long_operation("ats_parse_check_file", operation, wait_seconds, ctx)

    @server.tool()
    async def ats_parse_check_resume(
        resume_id: Annotated[str, Field(description="Resume id.")],
        ctx: Context,
        settings: SettingsArg = None,
        all_templates: Annotated[
            bool, Field(description="Check all seven templates instead of settings.template.")
        ] = False,
        content_language: ContentLanguageArg = None,
        render_locale: Annotated[
            RenderLocale | None,
            Field(description="Print locale for section headings (sets settings.lang)."),
        ] = None,
        detail: DetailArg = False,
        wait_seconds: WaitSeconds = None,
    ) -> dict[str, Any]:
        """Parse-check a stored resume exactly as its PDF download renders.

        Renders the resume with the template settings (or every template),
        extracts the text like an ATS and compares it with the resume data.
        Per template: passes (content_recall >= 0.95 and no fatal/high check
        failed), scores, content_recall, order_fidelity and failing checks.
        Needs the PDF renderer (see get_status.pdf_export_ready). Only one
        such check runs at a time; a busy server returns a retry hint.
        """
        segment = path_segment(resume_id, "resume_id")
        body: dict[str, Any] = {
            "settings": _template_settings(settings, render_locale=render_locale).model_dump(mode="json"),
            "all_templates": all_templates,
        }
        if content_language:
            body["content_language"] = content_language

        async def operation() -> dict[str, Any]:
            try:
                check = await _post_own_output_check(runtime, segment, body)
            except BridgeError as exc:
                if exc.status_code == 429:
                    raise _busy_error(exc) from exc
                raise
            result: dict[str, Any] = {
                "resume_id": check.resume_id,
                "render_locale": check.render_locale,
                "results": own_output_summary(check),
            }
            if detail:
                result["report"] = check.model_dump(mode="json")
            return result

        return await runtime.run_long_operation("ats_parse_check_resume", operation, wait_seconds, ctx)

    @server.tool()
    async def tailor_and_verify(
        resume_id: Annotated[str, Field(description="Source (usually master) resume id.")],
        job_id: Annotated[str, Field(description="Job id from add_jobs.")],
        ctx: Context,
        template: Annotated[
            TemplateId | None, Field(description="Template to verify with (shortcut for settings.template).")
        ] = None,
        settings: SettingsArg = None,
        min_content_recall: Annotated[
            float,
            Field(description="Minimum share of resume content an ATS must recover to pass.", ge=0, le=1),
        ] = DEFAULT_MIN_CONTENT_RECALL,
        prompt_id: Annotated[
            str | None, Field(description="Optional tailoring prompt id; defaults to the configured prompt.")
        ] = None,
        detail: DetailArg = False,
        wait_seconds: WaitSeconds = None,
    ) -> dict[str, Any]:
        """Tailor a resume to a job, save it, and parse-check the result.

        Runs preview -> confirm (creates the tailored resume and its tracker
        card) -> parse check of the tailored resume's PDF. Returns
        tailored_resume_id, application_id, keyword_score, content_recall,
        order_fidelity, parseability_score, content_score, failing_checks and
        passes (content_recall >= min_content_recall and no fatal/high check
        failed; column checks of two-column templates count as medium). The
        tailored resume is kept even when passes is false. Repeating the call
        with the same arguments joins the running task or returns its result.
        """
        path_segment(resume_id, "resume_id")
        path_segment(job_id, "job_id")
        merged = _template_settings(settings, template=template)
        check_body = {"settings": merged.model_dump(mode="json")}
        key_material = json.dumps(
            {
                "resume_id": resume_id,
                "job_id": job_id,
                "settings": check_body["settings"],
                "min_content_recall": min_content_recall,
                "prompt_id": prompt_id,
                "detail": detail,
            },
            sort_keys=True,
        )
        idempotency_key = "tailor_and_verify:" + hashlib.sha256(key_material.encode()).hexdigest()

        async def operation() -> dict[str, Any]:
            preview, preview_data = await request_preview(runtime, resume_id, job_id, prompt_id)
            confirmed = await confirm_preview(runtime, preview)
            tailored_id = confirmed["tailored_resume_id"]
            try:
                check = await _check_own_output_retrying(runtime, tailored_id, check_body)
            except ToolError as exc:
                raise ToolError(
                    f"The tailored resume was saved (tailored_resume_id={tailored_id}, "
                    f"application_id={confirmed['application_id']}), but its parse check "
                    f"failed: {exc} Run ats_parse_check_resume on it to retry."
                ) from exc
            [template_result] = check.results
            report = template_result.report
            if report is None:
                raise ToolError("The parse check returned no report. Please try again.")
            ats = preview_data.get("ats_score") or {}
            result: dict[str, Any] = {
                "tailored_resume_id": tailored_id,
                "application_id": confirmed["application_id"],
                "source_resume_id": confirmed["source_resume_id"],
                "job_id": confirmed["job_id"],
                "template": template_result.template,
                "keyword_score": ats.get("overall_score"),
                "min_content_recall": min_content_recall,
                **report_summary(report, min_content_recall),
            }
            warnings = [*(preview_data.get("warnings") or []), *confirmed["warnings"]]
            if warnings:
                result["warnings"] = warnings
            if detail:
                result["keyword_score_detail"] = ats or None
                result["report"] = check.model_dump(mode="json")
            return result

        return await runtime.run_long_operation(
            "tailor_and_verify", operation, wait_seconds, ctx, idempotency_key=idempotency_key
        )
