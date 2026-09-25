"""ATS parse-check tools and the tailor-then-verify workflow.

Every tool goes through the parse-check routes the web UI uses. Output is
summary first: a verdict plus the failing checks rendered in English
(``messages_en``); ``detail=true`` adds the full report(s).
"""

import asyncio
import hashlib
import json
import logging
from functools import partial
from typing import Annotated, Any

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from app.mcp.bridge import BridgeError, path_segment, upload_content_type
from app.mcp.runtime import MCPRuntime, WaitSeconds
from app.mcp.tools.resumes import decode_base64, fetch_resume, read_local_file
from app.mcp.tools.tailoring import confirm_preview, request_preview
from app.routers.parse_check import ContentLanguage
from app.services.ats_parse.messages_en import render_message
from app.services.ats_parse.own_output import OwnOutputParseCheck, TemplateSettings
from app.services.ats_parse.report import CheckResult, ParseCheckReport
from app.services.ats_parse.templates import RenderLocale, TemplateId

logger = logging.getLogger(__name__)

DEFAULT_MIN_CONTENT_RECALL = 0.95
# A failed check at these severities fails the verdict. The engine already
# weighs multi_column/sidebar as medium for two-column-by-design templates.
BLOCKING_SEVERITIES = frozenset({"fatal", "high"})
# tailor_and_verify retries a parse check refused as busy (429) this many
# times, waiting the route's Retry-After (capped) between attempts.
PARSE_CHECK_BUSY_ATTEMPTS = 3
PARSE_CHECK_BUSY_MAX_WAIT_SECONDS = 30.0
DEFAULT_RETRY_AFTER_SECONDS = 10.0

# Default (non-detail) output is bounded by construction, whatever the report
# holds: at most this many failing checks per summary (most severe first,
# then a count of the rest), list params rendered as their first items plus
# "and N more", and clipped messages and warnings. detail=true returns the
# full data. tests/integration/test_mcp_ats_tools.py asserts a worst case
# stays under 2 KB for every tool.
SUMMARY_MAX_FAILING_CHECKS = 3
COMPACT_MAX_FAILING_CHECKS = 2
CHECK_ID_MAX_CHARS = 32
MESSAGE_LIST_ITEMS = 5
PARAM_ITEM_MAX_CHARS = 40
MESSAGE_MAX_CHARS = 200
REASON_MAX_IDS = 3
SUMMARY_MAX_WARNINGS = 2
WARNING_MAX_CHARS = 160
SEVERITY_RANK = {"fatal": 0, "high": 1, "medium": 2, "low": 3}

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


def _cap_list(values: list[Any], limit: int) -> list[Any]:
    """First ``limit`` items, then an ``and N more`` marker."""
    if len(values) <= limit:
        return values
    return [*values[:limit], f"and {len(values) - limit} more"]


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _bounded_params(params: dict[str, Any]) -> dict[str, Any]:
    """Params with list values capped and strings clipped for summaries."""
    bounded: dict[str, Any] = {}
    for key, value in params.items():
        if isinstance(value, list):
            value = [
                _clip(item, PARAM_ITEM_MAX_CHARS) if isinstance(item, str) else item
                for item in _cap_list(value, MESSAGE_LIST_ITEMS)
            ]
        elif isinstance(value, str):
            value = _clip(value, PARAM_ITEM_MAX_CHARS)
        bounded[key] = value
    return bounded


def _failed_by_severity(report: ParseCheckReport) -> list[CheckResult]:
    """Failed checks, most severe first (engine order within a severity)."""
    failed = [check for check in report.checks if check.status == "fail"]
    return sorted(failed, key=lambda check: SEVERITY_RANK[check.severity])


def failing_checks(
    report: ParseCheckReport, limit: int = SUMMARY_MAX_FAILING_CHECKS
) -> tuple[list[dict[str, Any]], int]:
    """The ``limit`` most severe failed checks, explained in English.

    Returns the items and how many further failed checks were left out.
    Messages are rendered from bounded params (lists capped, strings
    clipped) and clipped themselves, so each item has a bounded size.
    """
    failed = _failed_by_severity(report)
    items: list[dict[str, Any]] = []
    for check in failed[:limit]:
        item: dict[str, Any] = {"id": _clip(check.id, CHECK_ID_MAX_CHARS), "severity": check.severity}
        bounded = check.model_copy(update={"params": _bounded_params(check.params)})
        try:
            message = render_message(bounded)
        except (KeyError, IndexError, ValueError):
            # A check id or parameter the English catalog does not know yet:
            # show the raw parameters instead of failing the tool.
            logger.warning("No English message for parse check %r", check.id)
            message = "No English message for this check; params: " + json.dumps(
                bounded.params, sort_keys=True, default=str
            )
        item["message"] = _clip(message, MESSAGE_MAX_CHARS)
        if check.params.get("expected_by_template") is True:
            item["expected_by_template"] = True
        items.append(item)
    return items, max(len(failed) - limit, 0)


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
        _clip(check.id, CHECK_ID_MAX_CHARS)
        for check in _failed_by_severity(report)
        if check.severity in BLOCKING_SEVERITIES
    ]
    if blocking:
        reasons.append(
            "fatal/high checks failed: " + ", ".join(_cap_list(blocking, REASON_MAX_IDS))
        )
    return not reasons, reasons


def _scores(report: ParseCheckReport, min_content_recall: float) -> dict[str, Any]:
    passes, _ = verdict(report, min_content_recall)
    scores: dict[str, Any] = {
        "passes": passes,
        "parseability_score": report.overall_score,
        "content_score": report.content_score,
    }
    if report.roundtrip is not None:
        scores["content_recall"] = report.roundtrip.content_recall
        scores["order_fidelity"] = report.roundtrip.order_fidelity
    return scores


def report_summary(report: ParseCheckReport, min_content_recall: float) -> dict[str, Any]:
    """Bounded verdict of one report: scores, recall and top failing checks."""
    summary = _scores(report, min_content_recall)
    items, more = failing_checks(report)
    summary["failing_checks"] = items
    if more:
        summary["more_failing_checks"] = more
    _, reasons = verdict(report, min_content_recall)
    if reasons:
        summary["reasons"] = reasons
    return summary


def compact_report_summary(report: ParseCheckReport) -> dict[str, Any]:
    """One row of the all-templates comparison: scores and top failing ids.

    ``content_score`` is left out: content checks read the same text on every
    template, so it does not help compare them (a single-template check or
    detail=true has it).
    """
    summary = _scores(report, DEFAULT_MIN_CONTENT_RECALL)
    del summary["content_score"]
    failed = _failed_by_severity(report)
    if failed:
        summary["top_failing_checks"] = [
            f"{_clip(check.id, CHECK_ID_MAX_CHARS)} ({check.severity})"
            for check in failed[:COMPACT_MAX_FAILING_CHECKS]
        ]
    if len(failed) > COMPACT_MAX_FAILING_CHECKS:
        summary["more_failing_checks"] = len(failed) - COMPACT_MAX_FAILING_CHECKS
    return summary


def own_output_summary(check: OwnOutputParseCheck, *, compact: bool) -> list[dict[str, Any]]:
    """Per-template summaries of an own-output check.

    A single template gets the full summary (``report_summary``). With
    ``compact`` (all templates) each template is one comparison row: verdict,
    parseability, recall and ``top_failing_checks`` ("id (severity)" strings of
    its most severe failures), without messages or content_score; check a
    template on its own, or pass detail=true, for explanations. Templates that
    could not be checked carry ``status`` and ``error`` instead of a verdict.
    """
    results: list[dict[str, Any]] = []
    for result in check.results:
        entry: dict[str, Any] = {"template": result.template}
        if result.expected_by_template:
            entry["two_column_by_design"] = True
        if result.report is None:
            entry["status"] = result.status
            entry["error"] = result.error
        elif compact:
            entry.update(compact_report_summary(result.report))
        else:
            entry.update(report_summary(result.report, DEFAULT_MIN_CONTENT_RECALL))
        results.append(entry)
    return results


def bounded_warnings(warnings: list[str]) -> list[str]:
    """First ``SUMMARY_MAX_WARNINGS`` warnings, clipped, then ``and N more``."""
    return _cap_list([_clip(str(text), WARNING_MAX_CHARS) for text in warnings], SUMMARY_MAX_WARNINGS)


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


VERIFY_RESULT_IDS = (
    "tailored_resume_id",
    "application_id",
    "source_resume_id",
    "job_id",
    "template",
    "keyword_score",
)


def present_verification(
    stored: dict[str, Any], *, detail: bool, min_content_recall: float
) -> dict[str, Any]:
    """Project a stored tailor_and_verify result for one call.

    The task keeps the full result; the verdict is recomputed for the
    caller's ``min_content_recall`` and the full reports are included only
    with ``detail``.
    """
    check = OwnOutputParseCheck.model_validate(stored["report"])
    report = check.results[0].report
    if report is None:
        raise ToolError("The parse check returned no report. Please try again.")
    result: dict[str, Any] = {key: stored.get(key) for key in VERIFY_RESULT_IDS}
    result["min_content_recall"] = min_content_recall
    result.update(report_summary(report, min_content_recall))
    if stored.get("warnings"):
        result["warnings"] = stored["warnings"] if detail else bounded_warnings(stored["warnings"])
    if detail:
        result["keyword_score_detail"] = stored.get("keyword_score_detail")
        result["report"] = stored["report"]
    return result


async def _verify_tailored(
    runtime: MCPRuntime,
    confirmed: dict[str, Any],
    preview_data: dict[str, Any],
    check_body: dict[str, Any],
) -> dict[str, Any]:
    """Parse-check a confirmed tailored resume and build the stored result."""
    check = await _check_own_output_retrying(runtime, confirmed["tailored_resume_id"], check_body)
    if len(check.results) != 1 or check.results[0].report is None:
        raise ToolError("The parse check returned no report.")
    ats = preview_data.get("ats_score") or {}
    stored: dict[str, Any] = {
        "tailored_resume_id": confirmed["tailored_resume_id"],
        "application_id": confirmed["application_id"],
        "source_resume_id": confirmed["source_resume_id"],
        "job_id": confirmed["job_id"],
        "template": check.results[0].template,
        "keyword_score": ats.get("overall_score"),
        "keyword_score_detail": ats or None,
        "warnings": [*(preview_data.get("warnings") or []), *confirmed["warnings"]],
        "report": check.model_dump(mode="json"),
    }
    # Fail inside the task (with the saved ids) if the result cannot be shown.
    present_verification(stored, detail=False, min_content_recall=DEFAULT_MIN_CONTENT_RECALL)
    return stored


async def _source_fingerprint(runtime: MCPRuntime, resume_id: str, job_id: str) -> str:
    """Hash of the source resume's content and the job text.

    Part of the idempotency key, so a repeat call after either was edited
    tailors again. Jobs have no ``updated_at``, and a resume's changes with
    non-content writes, so content is hashed instead.
    """
    resume = await fetch_resume(runtime, resume_id)
    job = await runtime.bridge.get_json(f"/jobs/{path_segment(job_id, 'job_id')}")
    material = {
        "processed_resume": resume.get("processed_resume"),
        "raw_resume": (resume.get("raw_resume") or {}).get("content"),
        "job": job.get("content"),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


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
                "results": own_output_summary(check, compact=all_templates),
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
        for the same resume, job, settings and prompt (and unchanged resume and
        job content) joins the running task or returns its stored result,
        re-judged with this call's min_content_recall and detail.
        """
        path_segment(resume_id, "resume_id")
        path_segment(job_id, "job_id")
        merged = _template_settings(settings, template=template)
        check_body = {"settings": merged.model_dump(mode="json")}
        # Only inputs that change what gets created belong in the key; detail
        # and min_content_recall only change how the stored result is shown.
        key_material = json.dumps(
            {
                "resume_id": resume_id,
                "job_id": job_id,
                "prompt_id": prompt_id,
                "settings": check_body["settings"],
                "source": await _source_fingerprint(runtime, resume_id, job_id),
            },
            sort_keys=True,
        )
        idempotency_key = "tailor_and_verify:" + hashlib.sha256(key_material.encode()).hexdigest()

        async def operation() -> dict[str, Any]:
            preview, preview_data = await request_preview(runtime, resume_id, job_id, prompt_id)
            confirmed = await confirm_preview(runtime, preview)
            try:
                return await _verify_tailored(runtime, confirmed, preview_data, check_body)
            except Exception as exc:
                if isinstance(exc, ToolError):
                    reason = str(exc)
                else:
                    logger.exception("tailor_and_verify parse check failed")
                    reason = "The parse check failed unexpectedly."
                raise ToolError(
                    f"The tailored resume was saved (tailored_resume_id="
                    f"{confirmed['tailored_resume_id']}, application_id="
                    f"{confirmed['application_id']}), but its parse check failed: {reason} "
                    "Run ats_parse_check_resume on it to retry."
                ) from exc

        return await runtime.run_long_operation(
            "tailor_and_verify",
            operation,
            wait_seconds,
            ctx,
            idempotency_key=idempotency_key,
            presenter=partial(
                present_verification, detail=detail, min_content_recall=min_content_recall
            ),
        )
