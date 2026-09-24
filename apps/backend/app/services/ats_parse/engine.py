"""Parse-check orchestration: extract, check, score, and build the report."""

from __future__ import annotations

import time
from typing import Any

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
from app.services.ats_parse.roundtrip import compute_roundtrip
from app.services.ats_parse.templates import TEMPLATE_LAYOUTS, localize_section_meta
from app.services.ats_parse.report import (
    EXTRACTED_TEXT_PREVIEW_CHARS,
    UNREAD_DOCUMENT_SCORE_CAP,
    CheckResult,
    Extractability,
    ParseCheckReport,
    content_score,
    overall_score,
)
from app.services.parser import run_bounded_document_worker

PARSE_CHECK_TIMEOUT_SECONDS = 60.0
# Parse checks are unauthenticated and CPU-bound; one at a time, on a limiter
# separate from resume-upload conversion so they can never starve uploads.
PARSE_CHECK_WORKERS = 1
_PARSE_CHECK_LIMITER = anyio.CapacityLimiter(PARSE_CHECK_WORKERS)

# Layout checks that a two-column-by-design template is expected to fail.
_EXPECTED_BY_TWO_COLUMN = frozenset({"multi_column", "sidebar"})
EXPECTED_BY_TEMPLATE_SEVERITY = "medium"

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
        content_score=None,
        checks=[check],
        profiles=[],
        extracted_text_preview="",
    )


def _mark_expected_by_template(checks: list[CheckResult]) -> list[CheckResult]:
    """Flag column checks of a two-column-by-design template and cap their severity.

    The failure is still reported (an ATS may read the columns out of order),
    but it is the chosen design rather than a defect, so it weighs as medium.
    """
    return [
        check.model_copy(
            update={
                "severity": EXPECTED_BY_TEMPLATE_SEVERITY,
                "params": {**check.params, "expected_by_template": True},
            }
        )
        if check.id in _EXPECTED_BY_TWO_COLUMN
        else check
        for check in checks
    ]


def build_report(
    document: ExtractedDocument,
    *,
    content_language: str | None = None,
    render_locale: str | None = None,
    deadline: float | None = None,
    template: str | None = None,
    roundtrip_source: dict[str, Any] | None = None,
) -> ParseCheckReport:
    """Run every check on an extracted document and assemble the report.

    Args:
        document: Output of ``extract_document``.
        content_language: Language of the resume text; auto-detected when None.
        render_locale: Locale used to render section headings, if known.
        deadline: ``time.monotonic()`` value after which analysis stops.
        template: Resume Matcher template that rendered the document, if any;
            selects its rendered-field map and two-column expectations.
        roundtrip_source: Payload the template rendered; when given, the
            report includes the round-trip self-consistency result.
    """
    if document.file_format == "doc":
        return _unsupported_report()
    language = content_language or detect_content_language(document.text)
    text_layer = has_text_layer(document)
    layout = TEMPLATE_LAYOUTS.get(template) if template else None
    checks = [
        *run_layout_checks(document, deadline),
        *run_content_checks(
            document.text,
            content_language=language,
            render_locale=render_locale,
            has_text=text_layer,
        ),
    ]
    if layout is not None and layout.two_column:
        checks = _mark_expected_by_template(checks)
    roundtrip = None
    if roundtrip_source is not None:
        roundtrip = compute_roundtrip(
            localize_section_meta(roundtrip_source, render_locale),
            document.text,
            rendered_fields=layout.rendered_fields if layout else None,
            personal_order=layout.personal_order if layout else None,
            body_order=layout.body_order if layout else None,
        )
    score = overall_score(checks)
    if not text_layer and document.dense_pages:
        # Every analyzed page was skipped as too dense: the score would only
        # reflect checks that had nothing to look at.
        score = min(score, UNREAD_DOCUMENT_SCORE_CAP)
    return ParseCheckReport(
        file_format=document.file_format,
        extractability=_extractability(checks),
        content_language=language,
        overall_score=score,
        content_score=content_score(checks),
        checks=checks,
        roundtrip=roundtrip,
        profiles=score_profiles(checks, detected_sections(document.text, render_locale)),
        extracted_text_preview=document.text[:EXTRACTED_TEXT_PREVIEW_CHARS],
    )


def check_document_sync(
    content: bytes,
    filename: str,
    content_language: str | None = None,
    render_locale: str | None = None,
    template: str | None = None,
    roundtrip_source: dict[str, Any] | None = None,
    timeout_seconds: float | None = None,
) -> ParseCheckReport:
    """Extract and check one document (blocking; run through the worker helper).

    The worker thread cannot be killed, so it enforces its own deadline
    (``PARSE_CHECK_TIMEOUT_SECONDS`` unless ``timeout_seconds`` is given).
    """
    deadline = time.monotonic() + (timeout_seconds or PARSE_CHECK_TIMEOUT_SECONDS)
    document = extract_document(content, filename, deadline=deadline)
    return build_report(
        document,
        content_language=content_language,
        render_locale=render_locale,
        deadline=deadline,
        template=template,
        roundtrip_source=roundtrip_source,
    )


async def run_parse_check(
    content: bytes,
    filename: str,
    *,
    content_language: str | None = None,
    render_locale: str | None = None,
    template: str | None = None,
    roundtrip_source: dict[str, Any] | None = None,
    timeout_seconds: float | None = None,
) -> ParseCheckReport:
    """Parse-check a document under its own limiter and deadline (60 s default).

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
        template,
        roundtrip_source,
        timeout_seconds,
        timeout_seconds=timeout_seconds or PARSE_CHECK_TIMEOUT_SECONDS,
        limiter=_PARSE_CHECK_LIMITER,
        label="ATS parse check",
    )
