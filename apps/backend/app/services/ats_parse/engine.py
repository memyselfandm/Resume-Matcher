"""Parse-check orchestration: extract, check, score, and build the report."""

from __future__ import annotations

import time

import anyio

from app.services.ats_parse.content_checks import (
    UNKNOWN_LANGUAGE,
    detect_content_language,
    detected_sections,
    run_content_checks,
)
from app.services.ats_parse.extract import ExtractedDocument, extract_document
from app.services.ats_parse.layout_checks import has_text_layer, run_layout_checks
from app.services.ats_parse.profiles import score_profiles
from app.services.ats_parse.report import (
    EXTRACTED_TEXT_PREVIEW_CHARS,
    CheckResult,
    Extractability,
    ParseCheckReport,
    overall_score,
)
from app.services.parser import run_bounded_document_worker

PARSE_CHECK_TIMEOUT_SECONDS = 60.0
# Parse checks are unauthenticated and CPU-bound; one at a time, on a limiter
# separate from resume-upload conversion so they can never starve uploads.
PARSE_CHECK_WORKERS = 1
_PARSE_CHECK_LIMITER = anyio.CapacityLimiter(PARSE_CHECK_WORKERS)

# Extraction-quality failures that mean an ATS recovers only part of the text.
_PARTIAL_EXTRACTION_CHECKS = frozenset(
    {"unmapped_glyphs", "replacement_characters", "text_as_image", "truncated"}
)


def _extractability(checks: list[CheckResult]) -> Extractability:
    failed = {check.id for check in checks if check.status == "fail"}
    if "text_layer" in failed:
        return "none"
    if failed & _PARTIAL_EXTRACTION_CHECKS:
        return "partial"
    return "full"


def _unsupported_report() -> ParseCheckReport:
    check = CheckResult(
        id="file_format",
        category="extraction",
        severity="fatal",
        status="fail",
        params={"format": "doc", "supported": ["pdf", "docx"]},
    )
    return ParseCheckReport(
        file_format="doc",
        extractability="unsupported_format",
        content_language=UNKNOWN_LANGUAGE,
        overall_score=None,
        checks=[check],
        profiles=[],
        extracted_text_preview="",
    )


def build_report(
    document: ExtractedDocument,
    *,
    content_language: str | None = None,
    render_locale: str | None = None,
    deadline: float | None = None,
) -> ParseCheckReport:
    """Run every check on an extracted document and assemble the report.

    Args:
        document: Output of ``extract_document``.
        content_language: Language of the resume text; auto-detected when None.
        render_locale: Locale used to render section headings, if known.
        deadline: ``time.monotonic()`` value after which analysis stops.
    """
    if document.file_format == "doc":
        return _unsupported_report()
    language = content_language or detect_content_language(document.text)
    text_layer = has_text_layer(document)
    checks = [
        *run_layout_checks(document, deadline),
        *run_content_checks(
            document.text,
            content_language=language,
            render_locale=render_locale,
            has_text=text_layer,
        ),
    ]
    return ParseCheckReport(
        file_format=document.file_format,
        extractability=_extractability(checks),
        content_language=language,
        overall_score=overall_score(checks),
        checks=checks,
        profiles=score_profiles(checks, detected_sections(document.text, render_locale)),
        extracted_text_preview=document.text[:EXTRACTED_TEXT_PREVIEW_CHARS],
    )


def check_document_sync(
    content: bytes,
    filename: str,
    content_language: str | None = None,
    render_locale: str | None = None,
) -> ParseCheckReport:
    """Extract and check one document (blocking; run through the worker helper).

    The worker thread cannot be killed, so it enforces its own deadline.
    """
    deadline = time.monotonic() + PARSE_CHECK_TIMEOUT_SECONDS
    document = extract_document(content, filename, deadline=deadline)
    return build_report(
        document,
        content_language=content_language,
        render_locale=render_locale,
        deadline=deadline,
    )


async def run_parse_check(
    content: bytes,
    filename: str,
    *,
    content_language: str | None = None,
    render_locale: str | None = None,
) -> ParseCheckReport:
    """Parse-check a document under its own limiter and a 60 s deadline.

    Raises:
        DocumentValidationError: unreadable or unsupported container.
        DocumentResourceLimitError: decoding exceeded the expansion budget.
        TimeoutError: queueing plus analysis exceeded the deadline.
    """
    return await run_bounded_document_worker(
        check_document_sync,
        content,
        filename,
        content_language,
        render_locale,
        timeout_seconds=PARSE_CHECK_TIMEOUT_SECONDS,
        limiter=_PARSE_CHECK_LIMITER,
        label="ATS parse check",
    )
