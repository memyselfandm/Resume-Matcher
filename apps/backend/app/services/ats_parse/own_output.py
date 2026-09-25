"""Parse-check Resume Matcher's own output: render, extract, and round-trip.

"Own output" is exactly what ``GET /api/v1/resumes/{id}/pdf`` returns for a
full template settings object. The PDF and the payload the print page renders
(``GET /api/v1/resumes?resume_id=`` -> ``processed_resume``) are fetched
through the in-process API client, so the render path, its validation, and
its print URL are the ones users download, with no copy of either.

Renderer admission is fail-fast (``PDFRenderOverloadedError`` -> 503), never
queued, so each template render is retried with backoff while the overall
budget allows. A template that still cannot be rendered is reported as
``render_failed`` and the other templates' reports are still returned.

Renderer fairness: only one own-output check runs per process (a second one
is refused with ``OwnOutputBusyError`` instead of queueing), and its renders
are sequential, so it holds at most one renderer slot. Its renders are marked
as background work (``pdf.background_renders``): after a user download is
refused as busy, the check starts no render for ``DOWNLOAD_PRIORITY_SECONDS``,
so a user who retries within that window gets the slot instead of the next
template. With ``PDF_MAX_CONCURRENCY=1`` the only slot, when busy, is held by
a user download, so instead of failing after three attempts the check polls
every ``SINGLE_SLOT_POLL_SECONDS`` until the slot frees or the budget runs out.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

import anyio
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import ASGIApp

from app import pdf
from app.internal_client import InternalClient, InternalRequestError, path_segment
from app.pdf import RENDER_BUSY_MESSAGE, RENDER_TIMEOUT_MESSAGE
from app.services.ats_parse.engine import PARSE_CHECK_TIMEOUT_SECONDS, run_parse_check
from app.services.ats_parse.report import ParseCheckReport
from app.services.ats_parse.templates import (
    DEFAULT_RENDER_LOCALE,
    TEMPLATE_IDS,
    TEMPLATE_LAYOUTS,
    RenderLocale,
    TemplateId,
)
from app.services.parser import DocumentResourceLimitError, DocumentValidationError

logger = logging.getLogger(__name__)

SINGLE_TEMPLATE_BUDGET_SECONDS = 60.0
ALL_TEMPLATES_BUDGET_SECONDS = 200.0
MAX_RENDER_ATTEMPTS = 3
RENDER_RETRY_BACKOFF_SECONDS = (1.0, 2.0)
# With a single renderer slot, a busy renderer is polled at this interval
# until the budget runs out (a user render holds it for a bounded time).
SINGLE_SLOT_POLL_SECONDS = 1.0
# Budget kept for extraction after the last render of a template.
MIN_ANALYSIS_SECONDS = 5.0
# Extraction is not started with less time than this left.
MIN_EXTRACTION_SECONDS = 1.0
# After a user download is refused as busy, no render starts for this long.
DOWNLOAD_PRIORITY_SECONDS = 10.0
# One own-output check per process; others are refused, never queued.
MAX_CONCURRENT_CHECKS = 1
BUSY_RETRY_AFTER_SECONDS = 10
_active_checks = 0

SpacingLevel = int
FontFamily = Literal["serif", "sans-serif", "mono"]
TemplateStatus = Literal["ok", "render_failed", "timed_out"]
TemplateError = Literal[
    "render_busy", "render_timeout", "render_error", "analysis_error", "budget_exhausted"
]


class MarginSettings(BaseModel):
    """Page margins in millimetres (applied by the PDF renderer)."""

    model_config = ConfigDict(extra="forbid")

    top: int = Field(10, ge=5, le=25)
    bottom: int = Field(10, ge=5, le=25)
    left: int = Field(10, ge=5, le=25)
    right: int = Field(10, ge=5, le=25)


class SpacingSettings(BaseModel):
    """Spacing levels (1-5)."""

    model_config = ConfigDict(extra="forbid")

    section: SpacingLevel = Field(3, ge=1, le=5)
    item: SpacingLevel = Field(2, ge=1, le=5)
    lineHeight: SpacingLevel = Field(3, ge=1, le=5)


class FontSizeSettings(BaseModel):
    """Font scale levels (1-5) and families."""

    model_config = ConfigDict(extra="forbid")

    base: SpacingLevel = Field(3, ge=1, le=5)
    headerScale: SpacingLevel = Field(3, ge=1, le=5)
    headerFont: FontFamily = "serif"
    bodyFont: FontFamily = "sans-serif"


class TemplateSettings(BaseModel):
    """Frontend ``TemplateSettings`` plus the print locale.

    Defaults equal ``DEFAULT_TEMPLATE_SETTINGS`` in
    ``apps/frontend/lib/types/template-settings.ts``; together these are the
    17 query parameters of ``GET /api/v1/resumes/{id}/pdf``.
    """

    model_config = ConfigDict(extra="forbid")

    template: TemplateId = "swiss-single"
    pageSize: Literal["A4", "LETTER"] = "A4"
    margins: MarginSettings = Field(default_factory=MarginSettings)
    spacing: SpacingSettings = Field(default_factory=SpacingSettings)
    fontSize: FontSizeSettings = Field(default_factory=FontSizeSettings)
    compactMode: bool = False
    showContactIcons: bool = False
    accentColor: Literal["blue", "green", "orange", "red"] = "blue"
    lang: RenderLocale | None = None

    def pdf_query(self, template: str) -> dict[str, str | int]:
        """Query parameters of the PDF route for ``template``."""
        params: dict[str, str | int] = {
            "template": template,
            "pageSize": self.pageSize,
            "marginTop": self.margins.top,
            "marginBottom": self.margins.bottom,
            "marginLeft": self.margins.left,
            "marginRight": self.margins.right,
            "sectionSpacing": self.spacing.section,
            "itemSpacing": self.spacing.item,
            "lineHeight": self.spacing.lineHeight,
            "fontSize": self.fontSize.base,
            "headerScale": self.fontSize.headerScale,
            "headerFont": self.fontSize.headerFont,
            "bodyFont": self.fontSize.bodyFont,
            "compactMode": str(self.compactMode).lower(),
            "showContactIcons": str(self.showContactIcons).lower(),
            "accentColor": self.accentColor,
        }
        if self.lang:
            params["lang"] = self.lang
        return params


class TemplateParseCheck(BaseModel):
    """Parse-check outcome of one rendered template."""

    template: TemplateId
    status: TemplateStatus
    expected_by_template: bool
    render_attempts: int
    error: TemplateError | None = None
    report: ParseCheckReport | None = None


class OwnOutputParseCheck(BaseModel):
    """Parse-check of a stored resume as rendered by one or all templates."""

    resume_id: str
    render_locale: str
    settings: TemplateSettings
    results: list[TemplateParseCheck]


class OwnOutputBusyError(Exception):
    """Another own-output check is running in this process."""


class ResumeNotFoundError(Exception):
    """The resume does not exist."""


class ResumeNotProcessedError(Exception):
    """The resume has no structured data to render and compare with."""


class RenderUnavailableError(Exception):
    """A single-template check could not render or analyze its PDF."""

    def __init__(self, result: TemplateParseCheck) -> None:
        super().__init__(result.error or result.status)
        self.result = result


def _render_error_code(error: InternalRequestError) -> TemplateError:
    if str(error) == RENDER_BUSY_MESSAGE:
        return "render_busy"
    if str(error) == RENDER_TIMEOUT_MESSAGE:
        return "render_timeout"
    return "render_error"


async def _yield_to_downloads(
    deadline: float, sleep: Callable[[float], Awaitable[None]]
) -> bool:
    """Wait until no user download was refused in ``DOWNLOAD_PRIORITY_SECONDS``.

    Returns ``False`` when that wait would leave no budget for a render.
    """
    while True:
        elapsed = pdf.seconds_since_download_refusal()
        if elapsed is None or elapsed >= DOWNLOAD_PRIORITY_SECONDS:
            return True
        wait = DOWNLOAD_PRIORITY_SECONDS - elapsed
        if deadline - time.monotonic() - MIN_ANALYSIS_SECONDS <= wait:
            return False
        await sleep(wait)


async def _render_with_retry(
    client: InternalClient,
    resume_id: str,
    params: dict[str, str | int],
    deadline: float,
    sleep: Callable[[float], Awaitable[None]],
) -> tuple[bytes | None, int, TemplateError | None]:
    """Render one template; retry only a busy renderer, within the budget.

    A busy renderer is retried ``MAX_RENDER_ATTEMPTS`` times with backoff, or,
    with a single renderer slot (then held by a user download), polled until
    it frees. Every attempt first yields to recently refused user downloads.
    """
    single_slot = pdf.render_capacity() == 1
    error: TemplateError | None = None
    attempt = 0
    while True:
        attempt += 1
        if not await _yield_to_downloads(deadline, sleep):
            return None, attempt - 1, error or "budget_exhausted"
        remaining = deadline - time.monotonic() - MIN_ANALYSIS_SECONDS
        if remaining <= 0:
            return None, attempt - 1, error or "budget_exhausted"
        try:
            with anyio.fail_after(remaining), pdf.background_renders():
                response = await client.request("GET", f"/resumes/{resume_id}/pdf", params=params)
            return response.content, attempt, None
        except TimeoutError:
            return None, attempt, "budget_exhausted"
        except InternalRequestError as exc:
            if exc.status_code == 404:
                raise ResumeNotFoundError from exc
            if exc.status_code != 503:
                logger.warning("PDF render for parse check returned %s", exc.status_code)
                return None, attempt, "render_error"
            error = _render_error_code(exc)
            if error != "render_busy":
                return None, attempt, error
        if single_slot:
            backoff = SINGLE_SLOT_POLL_SECONDS
        elif attempt < MAX_RENDER_ATTEMPTS:
            backoff = RENDER_RETRY_BACKOFF_SECONDS[attempt - 1]
        else:
            return None, attempt, error
        if deadline - time.monotonic() - MIN_ANALYSIS_SECONDS <= backoff:
            return None, attempt, error
        await sleep(backoff)


def _failed(
    template: TemplateId, attempts: int, error: TemplateError, status: TemplateStatus
) -> TemplateParseCheck:
    return TemplateParseCheck(
        template=template,
        status=status,
        expected_by_template=TEMPLATE_LAYOUTS[template].two_column,
        render_attempts=attempts,
        error=error,
    )


async def _check_template(
    client: InternalClient,
    resume_id: str,
    source: dict[str, Any],
    template: TemplateId,
    settings: TemplateSettings,
    content_language: str,
    deadline: float,
    sleep: Callable[[float], Awaitable[None]],
) -> TemplateParseCheck:
    content, attempts, error = await _render_with_retry(
        client, resume_id, settings.pdf_query(template), deadline, sleep
    )
    if content is None:
        error = error or "render_error"
        status: TemplateStatus = "timed_out" if error == "budget_exhausted" else "render_failed"
        return _failed(template, attempts, error, status)
    remaining = deadline - time.monotonic()
    if remaining < MIN_EXTRACTION_SECONDS:
        return _failed(template, attempts, "budget_exhausted", "timed_out")
    try:
        report = await run_parse_check(
            content,
            "render.pdf",
            content_language=content_language,
            render_locale=settings.lang or DEFAULT_RENDER_LOCALE,
            template=template,
            roundtrip_source=source,
            timeout_seconds=min(remaining, PARSE_CHECK_TIMEOUT_SECONDS),
        )
    except TimeoutError:
        return _failed(template, attempts, "budget_exhausted", "timed_out")
    except (DocumentValidationError, DocumentResourceLimitError):
        logger.warning("Rendered PDF of template %s could not be analyzed", template)
        return _failed(template, attempts, "analysis_error", "render_failed")
    return TemplateParseCheck(
        template=template,
        status="ok",
        expected_by_template=TEMPLATE_LAYOUTS[template].two_column,
        render_attempts=attempts,
        report=report,
    )


async def _load_rendered_payload(client: InternalClient, resume_id: str) -> dict[str, Any]:
    try:
        body = await client.get_json("/resumes", params={"resume_id": resume_id})
    except InternalRequestError as exc:
        if exc.status_code == 404:
            raise ResumeNotFoundError from exc
        raise
    processed = (body.get("data") or {}).get("processed_resume")
    if not isinstance(processed, dict):
        raise ResumeNotProcessedError
    return processed


async def check_own_output(
    app: ASGIApp,
    resume_id: str,
    *,
    settings: TemplateSettings,
    content_language: str,
    all_templates: bool = False,
    sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
) -> OwnOutputParseCheck:
    """Render a stored resume like a user download and parse-check the PDF(s).

    Templates render one after another so a check never holds more than one
    renderer slot. Budgets: 60 s for one template, 200 s for all seven.

    Raises:
        InvalidIdentifierError: ``resume_id`` is not a safe id.
        OwnOutputBusyError: another own-output check is running.
        ResumeNotFoundError: no such resume.
        ResumeNotProcessedError: the resume has no structured data yet.
        RenderUnavailableError: single-template mode could not render or
            analyze the PDF (all-templates mode reports this per template).
    """
    global _active_checks
    resume_id = path_segment(resume_id, "resume id")
    if _active_checks >= MAX_CONCURRENT_CHECKS:
        raise OwnOutputBusyError
    _active_checks += 1
    try:
        results = await _run_checks(
            app, resume_id, settings, content_language, all_templates, sleep
        )
    finally:
        _active_checks -= 1
    if not all_templates and results[0].status != "ok":
        raise RenderUnavailableError(results[0])
    return OwnOutputParseCheck(
        resume_id=resume_id,
        render_locale=settings.lang or DEFAULT_RENDER_LOCALE,
        settings=settings,
        results=results,
    )


async def _run_checks(
    app: ASGIApp,
    resume_id: str,
    settings: TemplateSettings,
    content_language: str,
    all_templates: bool,
    sleep: Callable[[float], Awaitable[None]],
) -> list[TemplateParseCheck]:
    budget = ALL_TEMPLATES_BUDGET_SECONDS if all_templates else SINGLE_TEMPLATE_BUDGET_SECONDS
    deadline = time.monotonic() + budget
    templates: tuple[TemplateId, ...] = TEMPLATE_IDS if all_templates else (settings.template,)
    client = InternalClient(app)
    results: list[TemplateParseCheck] = []
    try:
        source = await _load_rendered_payload(client, resume_id)
        for template in templates:
            results.append(
                await _check_template(
                    client, resume_id, source, template, settings, content_language, deadline, sleep
                )
            )
    finally:
        await client.aclose()
    return results
