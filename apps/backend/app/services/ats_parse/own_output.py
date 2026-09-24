"""Parse-check Resume Matcher's own output: render, extract, and round-trip.

"Own output" is exactly what ``GET /api/v1/resumes/{id}/pdf`` returns for a
full template settings object. The PDF and the payload the print page renders
(``GET /api/v1/resumes?resume_id=`` -> ``processed_resume``) are fetched
through the in-process ASGI bridge, so the render path, its validation, and
its print URL are the ones users download, with no copy of either.

Renderer admission is fail-fast (``PDFRenderOverloadedError`` -> 503), never
queued, so each template render is retried with backoff while the overall
budget allows. A template that still cannot be rendered is reported as
``render_failed`` and the other templates' reports are still returned.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

import anyio
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import ASGIApp

from app.mcp.bridge import AppBridge, BridgeError, path_segment
from app.pdf import RENDER_BUSY_MESSAGE, RENDER_TIMEOUT_MESSAGE
from app.services.ats_parse.engine import run_parse_check
from app.services.ats_parse.report import ParseCheckReport
from app.services.ats_parse.templates import (
    DEFAULT_RENDER_LOCALE,
    TEMPLATE_IDS,
    TEMPLATE_LAYOUTS,
    RenderLocale,
    TemplateId,
)

logger = logging.getLogger(__name__)

SINGLE_TEMPLATE_BUDGET_SECONDS = 60.0
ALL_TEMPLATES_BUDGET_SECONDS = 200.0
MAX_RENDER_ATTEMPTS = 3
RENDER_RETRY_BACKOFF_SECONDS = (1.0, 2.0)
# Budget kept for extraction after the last render of a template.
MIN_ANALYSIS_SECONDS = 5.0

SpacingLevel = int
FontFamily = Literal["serif", "sans-serif", "mono"]
TemplateStatus = Literal["ok", "render_failed", "timed_out"]
TemplateError = Literal["render_busy", "render_timeout", "render_error", "budget_exhausted"]


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


class ResumeNotFoundError(Exception):
    """The resume does not exist."""


class ResumeNotProcessedError(Exception):
    """The resume has no structured data to render and compare with."""


class RenderUnavailableError(Exception):
    """A single-template check could not render or analyze its PDF."""

    def __init__(self, result: TemplateParseCheck) -> None:
        super().__init__(result.error or result.status)
        self.result = result


def _render_error_code(error: BridgeError) -> TemplateError:
    if str(error) == RENDER_BUSY_MESSAGE:
        return "render_busy"
    if str(error) == RENDER_TIMEOUT_MESSAGE:
        return "render_timeout"
    return "render_error"


async def _render_with_retry(
    bridge: AppBridge,
    resume_id: str,
    params: dict[str, str | int],
    deadline: float,
    sleep: Callable[[float], Awaitable[None]],
) -> tuple[bytes | None, int, TemplateError | None]:
    """Render one template; retry only a busy renderer, within the budget."""
    error: TemplateError | None = None
    for attempt in range(1, MAX_RENDER_ATTEMPTS + 1):
        remaining = deadline - time.monotonic() - MIN_ANALYSIS_SECONDS
        if remaining <= 0:
            return None, attempt - 1, error or "budget_exhausted"
        try:
            with anyio.fail_after(remaining):
                response = await bridge.request("GET", f"/resumes/{resume_id}/pdf", params=params)
            return response.content, attempt, None
        except TimeoutError:
            return None, attempt, "budget_exhausted"
        except BridgeError as exc:
            if exc.status_code == 404:
                raise ResumeNotFoundError from exc
            if exc.status_code != 503:
                logger.warning("PDF render for parse check returned %s", exc.status_code)
                return None, attempt, "render_error"
            error = _render_error_code(exc)
            if error != "render_busy":
                return None, attempt, error
        if attempt < MAX_RENDER_ATTEMPTS:
            backoff = RENDER_RETRY_BACKOFF_SECONDS[attempt - 1]
            if deadline - time.monotonic() - MIN_ANALYSIS_SECONDS <= backoff:
                return None, attempt, error
            await sleep(backoff)
    return None, MAX_RENDER_ATTEMPTS, error


async def _check_template(
    bridge: AppBridge,
    resume_id: str,
    source: dict[str, Any],
    template: TemplateId,
    settings: TemplateSettings,
    content_language: str,
    deadline: float,
    sleep: Callable[[float], Awaitable[None]],
) -> TemplateParseCheck:
    layout = TEMPLATE_LAYOUTS[template]
    pdf, attempts, error = await _render_with_retry(
        bridge, resume_id, settings.pdf_query(template), deadline, sleep
    )
    if pdf is None:
        status: TemplateStatus = "timed_out" if error == "budget_exhausted" else "render_failed"
        return TemplateParseCheck(
            template=template,
            status=status,
            expected_by_template=layout.two_column,
            render_attempts=attempts,
            error=error,
        )
    remaining = deadline - time.monotonic()
    try:
        report = await run_parse_check(
            pdf,
            "render.pdf",
            content_language=content_language,
            render_locale=settings.lang or DEFAULT_RENDER_LOCALE,
            template=template,
            roundtrip_source=source,
            timeout_seconds=max(remaining, 1.0),
        )
    except TimeoutError:
        return TemplateParseCheck(
            template=template,
            status="timed_out",
            expected_by_template=layout.two_column,
            render_attempts=attempts,
            error="budget_exhausted",
        )
    return TemplateParseCheck(
        template=template,
        status="ok",
        expected_by_template=layout.two_column,
        render_attempts=attempts,
        report=report,
    )


async def _load_rendered_payload(bridge: AppBridge, resume_id: str) -> dict[str, Any]:
    try:
        body = await bridge.get_json("/resumes", params={"resume_id": resume_id})
    except BridgeError as exc:
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
        ResumeNotFoundError: no such resume.
        ResumeNotProcessedError: the resume has no structured data yet.
        RenderUnavailableError: single-template mode could not render or
            analyze the PDF (all-templates mode reports this per template).
    """
    resume_id = path_segment(resume_id, "resume id")
    budget = ALL_TEMPLATES_BUDGET_SECONDS if all_templates else SINGLE_TEMPLATE_BUDGET_SECONDS
    deadline = time.monotonic() + budget
    templates: tuple[TemplateId, ...] = TEMPLATE_IDS if all_templates else (settings.template,)
    bridge = AppBridge(app)
    try:
        source = await _load_rendered_payload(bridge, resume_id)
        results = [
            await _check_template(
                bridge, resume_id, source, template, settings, content_language, deadline, sleep
            )
            for template in templates
        ]
    finally:
        await bridge.aclose()
    if not all_templates and results[0].status != "ok":
        raise RenderUnavailableError(results[0])
    return OwnOutputParseCheck(
        resume_id=resume_id,
        render_locale=settings.lang or DEFAULT_RENDER_LOCALE,
        settings=settings,
        results=results,
    )
