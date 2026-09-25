"""Bounded text and layout extraction for ATS parse checks.

PDF extraction runs pdfminer.six layout analysis through the shared bounded
parser, so every decoded stream is charged to the same 16MB expansion budget
used by resume uploads. The decode budget bounds bytes, not layout CPU, so
extraction also:

* disables pdfminer's hierarchical box grouping (``boxes_flow=None``), which
  is quadratic in the number of text boxes; text order comes from row
  reconstruction instead, so box order is never used;
* counts characters and drawing objects while a page is interpreted and
  abandons the page before layout analysis once ``MAX_PAGE_CHARS`` or
  ``MAX_PAGE_OBJECTS`` is exceeded, and stops interpreting further pages
  once the document-wide ``MAX_DOCUMENT_CHARS``/``MAX_DOCUMENT_OBJECTS``
  budget is spent (both reported through the ``truncated`` check);
* keeps at most ``MAX_LINES_PER_PAGE`` lines per page for geometric analysis;
* caps analyzed pages and extracted characters; and
* checks a cooperative monotonic deadline between pages and while
  interpreting, because worker threads cannot be killed. DOCX extraction reads the body, tables, text boxes,
headers/footers, and inline images with python-docx after the bounded
container validation used by uploads. Legacy ``.doc`` files are validated but
reported as an unsupported format.
"""

from __future__ import annotations

import io
import struct
import tempfile
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from docx import Document
from pdfminer.converter import PDFPageAggregator
from pdfminer.layout import (
    LAParams,
    LTChar,
    LTComponent,
    LTContainer,
    LTImage,
    LTPage,
    LTTextBox,
    LTTextLine,
)
from pdfminer.pdfexceptions import PDFException
from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
from pdfminer.pdfpage import PDFPage
from pdfminer.psexceptions import PSException

from app.services.parser import (
    DocumentResourceLimitError,
    DocumentValidationError,
    open_bounded_pdf,
    validate_doc_container,
    validate_docx_container,
)

FileFormat = Literal["pdf", "docx", "doc"]

MAX_ANALYZED_PAGES = 10
MAX_EXTRACTED_CHARS = 200_000
ROW_TOLERANCE_PT = 3.0
# A dense real resume page holds ~6k characters and a few hundred lines; the
# caps leave headroom for 7 pt resumes and long academic CVs.
MAX_PAGE_CHARS = 20_000
MAX_PAGE_OBJECTS = 25_000
# Interpretation cost is per glyph, so the whole document shares a budget too.
MAX_DOCUMENT_CHARS = 100_000
MAX_DOCUMENT_OBJECTS = 100_000
MAX_LINES_PER_PAGE = 600
_DEADLINE_CHECK_INTERVAL = 256

# Layout analysis results depend on these values, so they are pinned rather
# than inherited from pdfminer defaults that may change between releases.
# ``all_texts`` also groups text drawn inside form XObjects (LTFigure) into
# lines: Chromium draws semi-transparent text (CSS ``opacity``, e.g. the vivid
# template's surname) inside one, and text extractors do read it.
PINNED_LAPARAMS = LAParams(
    line_overlap=0.5,
    char_margin=2.0,
    line_margin=0.5,
    word_margin=0.1,
    boxes_flow=None,
    detect_vertical=False,
    all_texts=True,
)

SUPPORTED_SUFFIXES: dict[str, FileFormat] = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".doc": "doc",
}

_INVALID_DOCUMENT = "The uploaded file is not a valid PDF, DOC, or DOCX document."
_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_DRAWING_BLIP = "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
_COLUMNS_XPATH = f"{{{_WORD_NS}}}cols"


def check_deadline(deadline: float | None) -> None:
    """Raise ``TimeoutError`` once the monotonic ``deadline`` has passed."""
    if deadline is not None and time.monotonic() > deadline:
        raise TimeoutError("Parse check exceeded its deadline.")


class _PageTooDense(Exception):
    """Raised mid-interpretation when a page exceeds the per-page caps."""


class _DocumentBudgetExhausted(Exception):
    """Raised mid-interpretation when the whole document exceeds its budget."""


class _CappedAggregator(PDFPageAggregator):
    """Page aggregator that stops a page before layout analysis if it is too dense.

    pdfminer only analyzes layout in ``end_page``; raising while characters
    and drawing objects are still being collected keeps hostile pages from
    reaching the grouping stage at all.
    """

    def __init__(self, manager: PDFResourceManager, deadline: float | None) -> None:
        super().__init__(manager, laparams=PINNED_LAPARAMS)
        self._deadline = deadline
        self._chars = 0
        self._objects = 0
        self._document_chars = 0
        self._document_objects = 0

    def begin_page(self, page: PDFPage, ctm: Any) -> None:
        self._chars = 0
        self._objects = 0
        super().begin_page(page, ctm)

    def _count(self, *, char: bool) -> None:
        self._objects += 1
        self._document_objects += 1
        if char:
            self._chars += 1
            self._document_chars += 1
        if (
            self._document_chars > MAX_DOCUMENT_CHARS
            or self._document_objects > MAX_DOCUMENT_OBJECTS
        ):
            raise _DocumentBudgetExhausted
        if self._chars > MAX_PAGE_CHARS or self._objects > MAX_PAGE_OBJECTS:
            raise _PageTooDense
        if self._objects % _DEADLINE_CHECK_INTERVAL == 0:
            check_deadline(self._deadline)

    def render_char(self, *args: Any, **kwargs: Any) -> float:
        self._count(char=True)
        return super().render_char(*args, **kwargs)

    def paint_path(self, *args: Any, **kwargs: Any) -> None:
        self._count(char=False)
        super().paint_path(*args, **kwargs)

    def render_image(self, *args: Any, **kwargs: Any) -> None:
        self._count(char=False)
        super().render_image(*args, **kwargs)


@dataclass(frozen=True)
class TextLine:
    """One extracted text line with its page-space bounding box (points)."""

    x0: float
    y0: float
    x1: float
    y1: float
    text: str


@dataclass(frozen=True)
class PageLayout:
    """Layout facts for one analyzed PDF page."""

    number: int
    width: float
    height: float
    lines: tuple[TextLine, ...]
    image_sizes: tuple[tuple[float, float], ...]
    char_count: int


@dataclass(frozen=True)
class DocxFeatures:
    """Structural facts about a DOCX package that affect ATS extraction."""

    table_count: int
    text_box_text: str
    header_footer_text: str
    inline_image_count: int
    max_section_columns: int


@dataclass(frozen=True)
class ExtractedDocument:
    """Everything the parse checks need from one uploaded document."""

    file_format: FileFormat
    text: str
    pages: tuple[PageLayout, ...] = ()
    total_pages: int | None = None
    truncated_pages: bool = False
    truncated_chars: bool = False
    dense_pages: tuple[int, ...] = ()
    docx: DocxFeatures | None = None


def file_format_for(filename: str) -> FileFormat:
    """Map a filename to its supported document format or raise."""
    suffix = Path(filename).suffix.lower()
    file_format = SUPPORTED_SUFFIXES.get(suffix)
    if file_format is None:
        raise DocumentValidationError(_INVALID_DOCUMENT)
    return file_format


def _round(value: float) -> float:
    return round(value, 2)


# A glyph whose baseline rises more than this per unit of advance is rotated
# (pdfminer's ``upright`` only means "not mirrored").
ROTATION_TOLERANCE = 0.01


def _is_rotated(line: LTTextLine) -> bool:
    """Whether a line has glyphs drawn with a rotated baseline (e.g. a watermark).

    Only the baseline direction (``b`` against ``a`` of the glyph matrix)
    counts. A horizontal shear (``c``) is synthetic italic: Chromium on Linux
    slants fonts without an italic face that way, and that text stays in its
    row.
    """
    for char in line:
        if isinstance(char, LTChar):
            a, b, _, _, _, _ = char.matrix
            if abs(b) > ROTATION_TOLERANCE * max(abs(a), 1e-9):
                return True
    return False


def _walk_layout(
    item: LTComponent,
    lines: list[TextLine],
    images: list[tuple[float, float]],
    rotated: list[str],
) -> None:
    """Collect text lines and raster images in pdfminer's reading order.

    Rotated lines (diagonal watermarks, vertical side labels) keep their text
    in ``rotated`` but never join ``lines``: their bounding box spans the
    page diagonally, which would block every gutter band and scramble rows.
    """
    if isinstance(item, LTTextLine):
        text = item.get_text().replace("\n", " ").strip()
        if not text:
            return
        if _is_rotated(item):
            rotated.append(text)
            return
        lines.append(
            TextLine(_round(item.x0), _round(item.y0), _round(item.x1), _round(item.y1), text)
        )
        return
    if isinstance(item, LTImage):
        images.append((_round(item.width), _round(item.height)))
        return
    if isinstance(item, (LTTextBox, LTContainer)):
        for child in item:
            _walk_layout(child, lines, images, rotated)


def reconstruct_rows(lines: list[TextLine]) -> list[str]:
    """Join text lines into visual rows, top to bottom and left to right.

    Mirrors the line reconstruction of ats-screener ``pdf-parser.ts``
    (``reconstructLines``, MIT License, Copyright (c) 2026 Sunny Patel): items
    whose vertical centers are within ``ROW_TOLERANCE_PT`` form one row. A
    right-aligned date therefore stays on its title's row, while side-by-side
    columns interleave exactly as they do for line-based ATS extractors.
    """
    ordered = sorted(lines, key=lambda line: (-(line.y0 + line.y1) / 2, line.x0))
    rows: list[list[TextLine]] = []
    row_center = 0.0
    for line in ordered:
        center = (line.y0 + line.y1) / 2
        if rows and abs(row_center - center) <= ROW_TOLERANCE_PT:
            rows[-1].append(line)
        else:
            rows.append([line])
            row_center = center
    return [
        " ".join(item.text for item in sorted(row, key=lambda line: line.x0))
        for row in rows
    ]


# pdfminer signals malformed input with its own exceptions and a spread of
# builtin ones; anything else (including MemoryError) propagates.
_MALFORMED_PDF_ERRORS = (
    PDFException,
    PSException,
    ValueError,
    TypeError,
    KeyError,
    IndexError,
    AttributeError,
    AssertionError,
    struct.error,
    zlib.error,
    RecursionError,
)


def _extract_pdf(content: bytes, deadline: float | None) -> ExtractedDocument:
    pages: list[PageLayout] = []
    chunks: list[str] = []
    dense_pages: list[int] = []
    char_total = 0
    truncated_chars = False
    total_pages = 0
    try:
        document = open_bounded_pdf(io.BytesIO(content))
        manager = PDFResourceManager(caching=False)
        device = _CappedAggregator(manager, deadline)
        interpreter = PDFPageInterpreter(manager, device)
        for page in PDFPage.create_pages(document):
            total_pages += 1
            check_deadline(deadline)
            if total_pages > MAX_ANALYZED_PAGES or truncated_chars:
                continue
            try:
                interpreter.process_page(page)
            except _PageTooDense:
                dense_pages.append(total_pages)
                continue
            except _DocumentBudgetExhausted:
                # Later pages are counted but not interpreted.
                truncated_chars = True
                continue
            layout: LTPage = device.get_result()
            lines: list[TextLine] = []
            rotated: list[str] = []
            images: list[tuple[float, float]] = []
            for item in layout:
                _walk_layout(item, lines, images, rotated)
            page_chars = 0
            # Rotated text follows the page's rows, as its own rows.
            for row in [*reconstruct_rows(lines), *rotated]:
                remaining = MAX_EXTRACTED_CHARS - char_total
                if remaining <= 0:
                    truncated_chars = True
                    break
                text = row[:remaining]
                if len(text) < len(row):
                    truncated_chars = True
                chunks.append(text)
                char_total += len(text)
                page_chars += len(text.replace(" ", ""))
            if len(lines) > MAX_LINES_PER_PAGE:
                # Keep the text, skip geometric analysis of an abnormal page.
                dense_pages.append(total_pages)
                lines = []
            pages.append(
                PageLayout(
                    number=total_pages,
                    width=_round(layout.width),
                    height=_round(layout.height),
                    lines=tuple(lines),
                    image_sizes=tuple(images),
                    char_count=page_chars,
                )
            )
            chunks.append("")
    except (DocumentResourceLimitError, TimeoutError):
        raise
    except _MALFORMED_PDF_ERRORS as exc:
        raise DocumentValidationError(_INVALID_DOCUMENT) from exc
    if total_pages == 0:
        raise DocumentValidationError(_INVALID_DOCUMENT)
    return ExtractedDocument(
        file_format="pdf",
        text="\n".join(chunks).strip(),
        pages=tuple(pages),
        total_pages=total_pages,
        truncated_pages=total_pages > MAX_ANALYZED_PAGES,
        truncated_chars=truncated_chars,
        dense_pages=tuple(dense_pages),
    )


_TEXT_BOX_TAG = f"{{{_WORD_NS}}}txbxContent"
_TEXT_TAG = f"{{{_WORD_NS}}}t"
_BREAK_TAGS = frozenset({f"{{{_WORD_NS}}}br", f"{{{_WORD_NS}}}cr"})
_TAB_TAG = f"{{{_WORD_NS}}}tab"
_MC_FALLBACK_TAG = (
    "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
)


def _run_text(paragraph: Any, *, skip_text_boxes: bool) -> str:
    """Visible text of one ``w:p``, keeping line breaks and tabs as whitespace."""
    parts: list[str] = []
    for node in paragraph.iter(_TEXT_TAG, _TAB_TAG, *_BREAK_TAGS):
        if skip_text_boxes and any(
            ancestor.tag == _TEXT_BOX_TAG for ancestor in node.iterancestors()
        ):
            continue
        if node.tag == _TEXT_TAG:
            parts.append(node.text or "")
        elif node.tag == _TAB_TAG:
            parts.append("\t")
        else:
            parts.append("\n")
    return "".join(parts).strip()


def _paragraph_texts(element: Any) -> list[str]:
    """Return the visible text of every ``w:p`` below an lxml element."""
    texts: list[str] = []
    for paragraph in element.iter(f"{{{_WORD_NS}}}p"):
        text = _run_text(paragraph, skip_text_boxes=False)
        if text:
            texts.append(text)
    return texts


def _body_texts(body: Any) -> list[str]:
    """Body text in document order, excluding text-box content."""
    texts: list[str] = []
    for paragraph in body.iter(f"{{{_WORD_NS}}}p"):
        if any(
            ancestor.tag == _TEXT_BOX_TAG for ancestor in paragraph.iterancestors()
        ):
            continue
        text = _run_text(paragraph, skip_text_boxes=True)
        if text:
            texts.append(text)
    return texts


def _extract_docx(content: bytes) -> ExtractedDocument:
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(content)
        validate_docx_container(tmp_path)
        try:
            document = Document(str(tmp_path))
        except Exception as exc:
            raise DocumentValidationError(_INVALID_DOCUMENT) from exc
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

    body = document.element.body
    body_lines = _body_texts(body)
    text_box_lines: list[str] = []
    for text_box in body.iter(_TEXT_BOX_TAG):
        # Word stores each text box twice: DrawingML in mc:Choice and a VML
        # copy in mc:Fallback. Count the content once.
        if any(ancestor.tag == _MC_FALLBACK_TAG for ancestor in text_box.iterancestors()):
            continue
        text_box_lines.extend(_paragraph_texts(text_box))

    header_footer_lines: list[str] = []
    max_columns = 1
    for section in document.sections:
        for part in (
            section.header,
            section.footer,
            section.first_page_header,
            section.first_page_footer,
            section.even_page_header,
            section.even_page_footer,
        ):
            if part.is_linked_to_previous:
                continue
            header_footer_lines.extend(_paragraph_texts(part._element))
        for columns in section._sectPr.iter(_COLUMNS_XPATH):
            raw = columns.get(f"{{{_WORD_NS}}}num")
            if raw is not None and raw.isdigit():
                max_columns = max(max_columns, int(raw))

    # Every text field feeds regex checks, so each is capped independently.
    text = "\n".join(body_lines)
    text_box_text = "\n".join(text_box_lines)
    header_footer_text = "\n".join(dict.fromkeys(header_footer_lines))
    truncated = any(
        len(value) > MAX_EXTRACTED_CHARS
        for value in (text, text_box_text, header_footer_text)
    )
    return ExtractedDocument(
        file_format="docx",
        text=text[:MAX_EXTRACTED_CHARS],
        truncated_chars=truncated,
        docx=DocxFeatures(
            table_count=len(document.tables),
            text_box_text=text_box_text[:MAX_EXTRACTED_CHARS],
            header_footer_text=header_footer_text[:MAX_EXTRACTED_CHARS],
            inline_image_count=sum(1 for _ in body.iter(_DRAWING_BLIP)),
            max_section_columns=max_columns,
        ),
    )


def _validate_doc(content: bytes) -> ExtractedDocument:
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".doc", delete=False) as tmp:
            tmp_path = Path(tmp.name)
            tmp.write(content)
        validate_doc_container(tmp_path)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
    return ExtractedDocument(file_format="doc", text="")


def extract_document(
    content: bytes, filename: str, *, deadline: float | None = None
) -> ExtractedDocument:
    """Extract text and layout facts from a PDF/DOCX (blocking; run in a worker).

    Args:
        content: Raw document bytes.
        filename: Name used only for its extension.
        deadline: ``time.monotonic()`` value after which work stops.

    Raises:
        DocumentValidationError: the bytes are not a readable document.
        DocumentResourceLimitError: decoding exceeded the shared expansion budget.
        TimeoutError: the deadline passed.
    """
    file_format = file_format_for(filename)
    if file_format == "pdf":
        return _extract_pdf(content, deadline)
    if file_format == "docx":
        return _extract_docx(content)
    return _validate_doc(content)
