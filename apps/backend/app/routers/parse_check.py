"""ATS parse-check endpoints (parseability of a resume file, no persistence).

* ``POST /ats/parse-check``: an uploaded PDF/DOCX/DOC.
* ``POST /resumes/{resume_id}/parse-check``: Resume Matcher's own output, the
  PDF a user downloads for the given template settings, with round-trip
  self-consistency against the payload the template rendered.
"""

import logging
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from app.routers.resumes import (
    ALLOWED_TYPES,
    DOCUMENT_TYPES_BY_EXTENSION,
    MAX_FILE_SIZE,
    UPLOAD_READ_CHUNK_SIZE,
)
from app.config_cache import get_content_language
from app.mcp.bridge import InvalidIdentifierError
from app.services.ats_parse import ParseCheckReport, run_parse_check
from app.services.ats_parse.own_output import (
    OwnOutputParseCheck,
    RenderUnavailableError,
    ResumeNotFoundError,
    ResumeNotProcessedError,
    TemplateSettings,
    check_own_output,
)
from app.services.parser import (
    MAX_UNPACKED_DOCUMENT_BYTES,
    DocumentResourceLimitError,
    DocumentValidationError,
)

router = APIRouter(tags=["ATS Parse Check"])
logger = logging.getLogger(__name__)

ContentLanguage = Literal["en", "es", "fr", "pt", "de", "ja", "ko", "zh"]


def _validate_upload_type(file: UploadFile) -> None:
    """Require a PDF/DOC/DOCX MIME type that matches the filename extension."""
    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file type: {file.content_type}. Allowed: PDF, DOC, DOCX",
        )
    suffix = Path(file.filename or "").suffix.lower()
    if DOCUMENT_TYPES_BY_EXTENSION.get(suffix) != file.content_type:
        raise HTTPException(status_code=400, detail="Upload a valid PDF, DOC, or DOCX file.")


async def _read_bounded(file: UploadFile) -> bytes:
    """Read at most one byte beyond the upload limit, in bounded chunks."""
    content = bytearray()
    while len(content) <= MAX_FILE_SIZE:
        chunk = await file.read(min(UPLOAD_READ_CHUNK_SIZE, MAX_FILE_SIZE - len(content) + 1))
        if not chunk:
            return bytes(content)
        content.extend(chunk)
    raise HTTPException(
        status_code=413,
        detail=f"File too large. Maximum size: {MAX_FILE_SIZE // (1024 * 1024)}MB",
    )


@router.post("/ats/parse-check", response_model=ParseCheckReport)
async def parse_check_file(
    file: UploadFile = File(...),
    content_language: ContentLanguage | None = Form(default=None),
) -> ParseCheckReport:
    """Check whether an ATS-style extractor can recover a resume file's content.

    The file is analyzed in memory and never stored. ``content_language`` is
    the language the resume is written in; when omitted it is detected, and an
    uncertain detection disables English-only checks rather than guessing.
    """
    _validate_upload_type(file)
    content = await _read_bounded(file)
    if not content:
        raise HTTPException(status_code=400, detail="Empty file")
    filename = f"upload{Path(file.filename or '').suffix.lower()}"
    try:
        return await run_parse_check(content, filename, content_language=content_language)
    except DocumentResourceLimitError as exc:
        logger.warning("Parse check exceeded a document resource limit: %s", exc)
        raise HTTPException(
            status_code=413,
            detail=(
                "Document content is too large to process. Maximum expanded size is "
                f"{MAX_UNPACKED_DOCUMENT_BYTES // (1024 * 1024)}MB."
            ),
        ) from exc
    except DocumentValidationError as exc:
        logger.info("Parse check rejected an invalid document: %s", exc)
        raise HTTPException(
            status_code=422,
            detail="The uploaded file is not a valid PDF, DOC, or DOCX document.",
        ) from exc
    except TimeoutError as exc:
        logger.warning("Parse check exceeded its deadline")
        raise HTTPException(
            status_code=504,
            detail="Parse check timed out. Please try a simpler document.",
        ) from exc
    except Exception as exc:
        logger.exception("Parse check failed")
        raise HTTPException(
            status_code=500, detail="Parse check failed. Please try again."
        ) from exc


class OwnOutputParseCheckRequest(BaseModel):
    """Body of ``POST /resumes/{resume_id}/parse-check`` (every field optional)."""

    model_config = ConfigDict(extra="forbid")

    settings: TemplateSettings = Field(default_factory=TemplateSettings)
    content_language: ContentLanguage | None = None
    all_templates: bool = False


_RENDER_FAILURES: dict[str, tuple[int, str]] = {
    "render_busy": (503, "PDF renderer is busy. Please try again shortly."),
    "render_timeout": (504, "PDF rendering timed out. Please try again."),
    "render_error": (503, "PDF rendering failed. Please try again."),
    "budget_exhausted": (504, "Parse check timed out. Please try again."),
}


@router.post("/resumes/{resume_id}/parse-check", response_model=OwnOutputParseCheck)
async def parse_check_resume(
    resume_id: str,
    request: Request,
    body: OwnOutputParseCheckRequest | None = None,
) -> OwnOutputParseCheck:
    """Parse-check a stored resume exactly as its PDF download renders it.

    Renders ``GET /resumes/{id}/pdf`` with the full template settings (one
    template, or all seven with ``all_templates``), extracts each PDF, and
    compares the text with the payload the print page rendered. Nothing is
    stored. ``content_language`` defaults to the configured content language;
    ``settings.lang`` is the print locale that localizes section headings.
    With ``all_templates``, a template that cannot be rendered is reported as
    ``render_failed`` (or ``timed_out``) and the response is still 200.
    """
    options = body or OwnOutputParseCheckRequest()
    content_language = options.content_language or get_content_language()
    try:
        return await check_own_output(
            request.app,
            resume_id,
            settings=options.settings,
            content_language=content_language,
            all_templates=options.all_templates,
        )
    except (InvalidIdentifierError, ResumeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail="Resume not found") from exc
    except ResumeNotProcessedError as exc:
        raise HTTPException(
            status_code=409,
            detail="Resume has no structured data yet. Wait for processing to finish, then try again.",
        ) from exc
    except RenderUnavailableError as exc:
        status_code, detail = _RENDER_FAILURES[exc.result.error or "render_error"]
        logger.warning("Own-output parse check could not render: %s", exc.result.error)
        raise HTTPException(status_code=status_code, detail=detail) from exc
    except Exception as exc:
        logger.exception("Own-output parse check failed")
        raise HTTPException(
            status_code=500, detail="Parse check failed. Please try again."
        ) from exc
