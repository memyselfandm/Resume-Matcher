"""Layout and extraction-quality checks.

Ported in part from sunnypatell/ats-screener (MIT License, Copyright (c) 2026
Sunny Patel): the formatting signals of ``src/lib/engine/scorer/format-scorer.ts``
(columns, tables, images, page count) and the table heuristic of
``src/lib/engine/parser/pdf-parser.ts`` (``detectTables``). The x-cluster
column heuristic of ``pdf-parser.ts`` is replaced by a per-page gutter-band
detector because x-clustering flags single-column resumes with right-aligned
dates as multi-column.

Gutter-band detector (per page, on pdfminer text lines):
    A vertical whitespace band at least ``GUTTER_MIN_WIDTH_PT`` wide that no
    text line crosses for a contiguous vertical run, with a column-like block
    of text on each side of the band within that run. A column-like block is at
    least ``BLOCK_MIN_LINES`` vertically consecutive lines sharing a left edge
    whose right edges are ragged; right-aligned stacks (dates, locations) share
    their right edge instead and never qualify. ``multi_column`` fails when the
    run spans at least ``GUTTER_MIN_HEIGHT_RATIO`` of the page's text height.
    ``sidebar`` fails, regardless of run height, when one side of such a band
    is narrower than ``SIDEBAR_MAX_WIDTH_RATIO`` of the page width.

Thresholds were calibrated on the committed fixtures. The swiss-two-column
geometry (65:35 grid, 16 CSS px = 12 pt gap) leaves at most a ~15 pt band
where left-column lines run to the column edge; an 18 pt band (the design
plan's starting value) only finds the gutter where left lines happen to be
short (run 0.81 of text height on ``two_column.pdf``) and would miss a dense
or justified left column, while 6-12 pt bands cover the full height (1.00).
``GUTTER_MIN_WIDTH_PT`` is frozen at 9 pt. Single-column fixtures, including
swiss-single-style right-aligned dates, produce no candidate at any width from
6 to 18 pt, because a right-aligned date/location stack is not a column-like
block and full-width lines interrupt every band elsewhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.ats_parse.content_checks import has_email, has_phone
from app.services.ats_parse.extract import (
    MAX_ANALYZED_PAGES,
    MAX_EXTRACTED_CHARS,
    MAX_LINES_PER_PAGE,
    MAX_PAGE_CHARS,
    ExtractedDocument,
    PageLayout,
    TextLine,
    check_deadline,
)
from app.services.ats_parse.report import CheckResult

GUTTER_MIN_WIDTH_PT = 9.0
GUTTER_MIN_HEIGHT_RATIO = 0.40
SIDEBAR_MAX_WIDTH_RATIO = 0.35
SIDEBAR_MIN_HEIGHT_RATIO = 0.15
BLOCK_MIN_LINES = 3
BLOCK_EDGE_TOLERANCE_PT = 2.0
BLOCK_MAX_LINE_GAP_RATIO = 1.5

TABLE_MIN_ROWS = 3
TABLE_MIN_CELL_GAP_PT = 30.0
TABLE_ROW_Y_BUCKET_PT = 3.0

IMAGE_MIN_SIDE_PT = 50.0
TEXT_AS_IMAGE_MAX_CHARS = 200
MAX_RECOMMENDED_PAGES = 2
REPLACEMENT_MAX_RATIO = 0.01
EVIDENCE_SAMPLE_SIZE = 5

_CID_RE = re.compile(r"\(cid:(\d{1,6})\)")
_REPLACEMENT_OR_CONTROL_RE = re.compile(r"[\ufffd\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _is_private_use(char: str) -> bool:
    code = ord(char)
    return (
        0xE000 <= code <= 0xF8FF
        or 0xF0000 <= code <= 0xFFFFD
        or 0x100000 <= code <= 0x10FFFD
    )


@dataclass(frozen=True)
class GutterCandidate:
    """One whitespace band with a column-like block on each side."""

    x0: float
    x1: float
    run_bottom: float
    run_top: float
    left_width: float
    right_width: float


def _column_block(lines: list[TextLine]) -> list[TextLine] | None:
    """Return the longest left-aligned, ragged-right run of consecutive lines.

    Left edges are clustered greedily in one sorted pass (a new cluster starts
    once an edge is more than ``BLOCK_EDGE_TOLERANCE_PT`` right of the
    cluster's first edge), so each line is examined once: O(n log n).
    """
    best: list[TextLine] | None = None
    clusters: list[list[TextLine]] = []
    for line in sorted(lines, key=lambda item: item.x0):
        if clusters and line.x0 - clusters[-1][0].x0 <= BLOCK_EDGE_TOLERANCE_PT:
            clusters[-1].append(line)
        else:
            clusters.append([line])
    for cluster in clusters:
        if len(cluster) < BLOCK_MIN_LINES:
            continue
        chain: list[TextLine] = []
        for line in sorted(cluster, key=lambda item: -item.y1):
            if chain:
                previous = chain[-1]
                height = max(previous.y1 - previous.y0, line.y1 - line.y0, 1.0)
                if previous.y0 - line.y1 > BLOCK_MAX_LINE_GAP_RATIO * height:
                    chain = []
            chain.append(line)
            if len(chain) >= BLOCK_MIN_LINES and (best is None or len(chain) > len(best)):
                right_edges = [item.x1 for item in chain]
                if max(right_edges) - min(right_edges) > BLOCK_EDGE_TOLERANCE_PT:
                    best = list(chain)
    return best


def _free_runs(
    blockers: list[tuple[float, float]], bottom: float, top: float
) -> list[tuple[float, float]]:
    """Vertical intervals of [bottom, top] not covered by any blocker interval."""
    runs: list[tuple[float, float]] = []
    cursor = top
    for y0, y1 in sorted(blockers, key=lambda interval: -interval[1]):
        if y1 < cursor and cursor - y1 > 0:
            runs.append((max(y1, bottom), cursor))
        cursor = min(cursor, y0)
        if cursor <= bottom:
            break
    if cursor > bottom:
        runs.append((bottom, cursor))
    return [run for run in runs if run[1] - run[0] > 0]


def find_gutters(
    page: PageLayout, deadline: float | None = None
) -> list[GutterCandidate]:
    """Scan a page for whitespace bands that separate two column-like blocks.

    Only runs at least ``SIDEBAR_MIN_HEIGHT_RATIO`` of the text height are
    examined; shorter side-by-side blocks (label/value skill grids, date
    rows) are never columns or sidebars.
    """
    lines = list(page.lines)
    if len(lines) < 2 * BLOCK_MIN_LINES:
        return []
    bottom = min(line.y0 for line in lines)
    top = max(line.y1 for line in lines)
    min_run = SIDEBAR_MIN_HEIGHT_RATIO * (top - bottom)
    left_edge = min(line.x0 for line in lines)
    right_edge = max(line.x1 for line in lines)
    content_right = max(right_edge, page.width - left_edge)
    candidates: list[GutterCandidate] = []
    # Adjacent scan positions usually see identical line sets; memoize blocks.
    block_cache: dict[tuple[int, ...], list[TextLine] | None] = {}

    def block_for(indices: tuple[int, ...]) -> list[TextLine] | None:
        if len(indices) < BLOCK_MIN_LINES:
            return None
        if indices not in block_cache:
            block_cache[indices] = _column_block([lines[index] for index in indices])
        return block_cache[indices]

    # Bands start only at line right edges (plus the text's left edge). Sliding
    # a band left to the nearest right edge never adds a blocking line, so
    # these positions dominate every other x, and the scan costs O(lines)
    # positions no matter how wide an attacker makes the page.
    positions = sorted({left_edge, *(line.x1 for line in lines)})
    for x in positions:
        if x + GUTTER_MIN_WIDTH_PT > right_edge:
            break
        check_deadline(deadline)
        band_end = x + GUTTER_MIN_WIDTH_PT
        blockers = [
            (line.y0, line.y1) for line in lines if line.x0 < band_end and line.x1 > x
        ]
        for run_bottom, run_top in _free_runs(blockers, bottom, top):
            if run_top - run_bottom < min_run:
                continue
            inside = [
                index
                for index, line in enumerate(lines)
                if run_bottom <= (line.y0 + line.y1) / 2 <= run_top
            ]
            left = block_for(tuple(i for i in inside if lines[i].x1 <= x))
            if left is None:
                continue
            right = block_for(tuple(i for i in inside if lines[i].x0 >= band_end))
            if right is None:
                continue
            # A side's width is the room between the band and the text edge,
            # not the width of its (possibly short) lines. Margins are assumed
            # symmetric because a short right column never reaches its edge.
            candidates.append(
                GutterCandidate(
                    x0=round(x, 1),
                    x1=round(band_end, 1),
                    run_bottom=round(run_bottom, 1),
                    run_top=round(run_top, 1),
                    left_width=round(x - left_edge, 1),
                    right_width=round(content_right - band_end, 1),
                )
            )
    return candidates


def _text_height(page: PageLayout) -> float:
    if not page.lines:
        return 0.0
    return max(line.y1 for line in page.lines) - min(line.y0 for line in page.lines)


def _column_checks(
    document: ExtractedDocument, deadline: float | None
) -> list[CheckResult]:
    if document.file_format == "docx":
        columns = document.docx.max_section_columns if document.docx else 1
        return [
            CheckResult(
                id="multi_column",
                category="layout",
                severity="high",
                status="fail" if columns > 1 else "pass",
                params={"section_columns": columns},
            ),
            CheckResult(
                id="sidebar",
                category="layout",
                severity="medium",
                status="not_applicable",
                params={"reason": "docx"},
            ),
        ]

    column_pages: list[dict[str, float | int]] = []
    sidebar_pages: list[dict[str, float | int]] = []
    for page in document.pages:
        candidates = find_gutters(page, deadline)
        if not candidates:
            continue
        text_height = _text_height(page)
        tall = [
            candidate
            for candidate in candidates
            if text_height > 0
            and (candidate.run_top - candidate.run_bottom) / text_height
            >= GUTTER_MIN_HEIGHT_RATIO
        ]
        if tall:
            best = max(tall, key=lambda c: (c.run_top - c.run_bottom, -c.x0))
            column_pages.append(
                {
                    "page": page.number,
                    "gutter_x0": best.x0,
                    "gutter_x1": best.x1,
                    "height_ratio": round((best.run_top - best.run_bottom) / text_height, 3),
                }
            )
        narrow_limit = SIDEBAR_MAX_WIDTH_RATIO * page.width
        narrow = [
            candidate
            for candidate in candidates
            if min(candidate.left_width, candidate.right_width) < narrow_limit
        ]
        if narrow:
            best = max(narrow, key=lambda c: (c.run_top - c.run_bottom, -c.x0))
            side = "left" if best.left_width < best.right_width else "right"
            sidebar_pages.append(
                {
                    "page": page.number,
                    "side": side,
                    "width_ratio": round(
                        min(best.left_width, best.right_width) / page.width, 3
                    ),
                }
            )
    # A sidebar on a page already failing multi_column is the same defect;
    # report it once (no double penalty) and record the suppression.
    column_page_numbers = [entry["page"] for entry in column_pages]
    suppressed = [entry["page"] for entry in sidebar_pages if entry["page"] in column_page_numbers]
    sidebar_pages = [entry for entry in sidebar_pages if entry["page"] not in column_page_numbers]
    if sidebar_pages:
        sidebar_status = "fail"
    elif suppressed:
        sidebar_status = "not_applicable"
    else:
        sidebar_status = "pass"
    sidebar_params: dict[str, object] = {"pages": [entry["page"] for entry in sidebar_pages]}
    if suppressed:
        sidebar_params["suppressed_pages"] = suppressed
    if sidebar_status == "not_applicable":
        sidebar_params["reason"] = "covered_by_multi_column"
    return [
        CheckResult(
            id="multi_column",
            category="layout",
            severity="high",
            status="fail" if column_pages else "pass",
            params={"pages": column_page_numbers},
            evidence={"pages": column_pages},
        ),
        CheckResult(
            id="sidebar",
            category="layout",
            severity="medium",
            status=sidebar_status,  # type: ignore[arg-type]
            params=sidebar_params,
            evidence={"pages": sidebar_pages},
        ),
    ]


def _pdf_table_rows(page: PageLayout) -> int:
    """Count rows with three or more cells separated by wide gaps (ats-screener)."""
    rows: dict[int, list[TextLine]] = {}
    for line in page.lines:
        bucket = round(line.y0 / TABLE_ROW_Y_BUCKET_PT)
        rows.setdefault(bucket, []).append(line)
    table_rows = 0
    for cells in rows.values():
        if len(cells) < 3:
            continue
        ordered = sorted(cells, key=lambda line: line.x0)
        gaps = [
            current.x0 - previous.x1
            for previous, current in zip(ordered, ordered[1:], strict=False)
        ]
        if sum(1 for gap in gaps if gap > TABLE_MIN_CELL_GAP_PT) >= 2:
            table_rows += 1
    return table_rows


def _table_check(document: ExtractedDocument) -> CheckResult:
    if document.file_format == "docx":
        count = document.docx.table_count if document.docx else 0
        return CheckResult(
            id="tables",
            category="layout",
            severity="medium",
            status="fail" if count else "pass",
            params={"count": count},
        )
    pages = [
        page.number
        for page in document.pages
        if _pdf_table_rows(page) >= TABLE_MIN_ROWS
    ]
    return CheckResult(
        id="tables",
        category="layout",
        severity="medium",
        status="fail" if pages else "pass",
        params={"pages": pages},
    )


def _image_checks(document: ExtractedDocument) -> list[CheckResult]:
    if document.file_format == "docx":
        count = document.docx.inline_image_count if document.docx else 0
        return [
            CheckResult(
                id="images",
                category="layout",
                severity="low",
                status="fail" if count else "pass",
                params={"count": count},
            ),
            CheckResult(
                id="text_as_image",
                category="extraction",
                severity="high",
                status="not_applicable",
                params={"reason": "docx"},
            ),
        ]
    image_pages: list[int] = []
    image_text_pages: list[int] = []
    for page in document.pages:
        large = [
            size
            for size in page.image_sizes
            if size[0] > IMAGE_MIN_SIDE_PT and size[1] > IMAGE_MIN_SIDE_PT
        ]
        if not large:
            continue
        image_pages.append(page.number)
        if page.char_count < TEXT_AS_IMAGE_MAX_CHARS:
            image_text_pages.append(page.number)
    return [
        CheckResult(
            id="images",
            category="layout",
            severity="low",
            status="fail" if image_pages else "pass",
            params={"pages": image_pages},
        ),
        CheckResult(
            id="text_as_image",
            category="extraction",
            severity="high",
            status="fail" if image_text_pages else "pass",
            params={"pages": image_text_pages, "max_chars": TEXT_AS_IMAGE_MAX_CHARS},
        ),
    ]


def _glyph_checks(text: str) -> list[CheckResult]:
    cid_matches = _CID_RE.findall(text)
    cid_sample = sorted({int(value) for value in cid_matches})[:EVIDENCE_SAMPLE_SIZE]
    private_use = [char for char in text if _is_private_use(char)]
    private_sample = [
        f"U+{code:04X}" for code in sorted({ord(char) for char in private_use})
    ][:EVIDENCE_SAMPLE_SIZE]
    visible = _CID_RE.sub("", text)
    bad = _REPLACEMENT_OR_CONTROL_RE.findall(visible)
    denominator = max(1, sum(1 for char in visible if not char.isspace()))
    ratio = round(len(bad) / denominator, 4)
    return [
        CheckResult(
            id="unmapped_glyphs",
            category="extraction",
            severity="high",
            status="fail" if cid_matches else "pass",
            params={"count": len(cid_matches)},
            evidence={"cids": cid_sample} if cid_matches else {},
        ),
        CheckResult(
            id="icon_font_glyphs",
            category="extraction",
            severity="medium",
            status="fail" if private_use else "pass",
            params={"count": len(private_use)},
            evidence={"codepoints": private_sample} if private_use else {},
        ),
        CheckResult(
            id="replacement_characters",
            category="extraction",
            severity="medium",
            status="fail" if ratio > REPLACEMENT_MAX_RATIO else "pass",
            params={"count": len(bad), "ratio": ratio, "max_ratio": REPLACEMENT_MAX_RATIO},
        ),
    ]


def _header_footer_contact(document: ExtractedDocument) -> CheckResult:
    if document.file_format != "docx" or document.docx is None:
        return CheckResult(
            id="header_footer_contact",
            category="layout",
            severity="high",
            status="not_applicable",
            params={"reason": document.file_format},
        )
    header = document.docx.header_footer_text
    fields = [
        name
        for name, detect in (("email", has_email), ("phone", has_phone))
        if detect(header) and not detect(document.text)
    ]
    return CheckResult(
        id="header_footer_contact",
        category="layout",
        severity="high",
        status="fail" if fields else "pass",
        params={"fields": fields},
    )


def _text_box_check(document: ExtractedDocument) -> CheckResult:
    if document.file_format != "docx" or document.docx is None:
        return CheckResult(
            id="text_boxes",
            category="layout",
            severity="medium",
            status="not_applicable",
            params={"reason": document.file_format},
        )
    chars = len(document.docx.text_box_text)
    return CheckResult(
        id="text_boxes",
        category="layout",
        severity="medium",
        status="fail" if chars else "pass",
        params={"chars": chars},
    )


def _page_count_check(document: ExtractedDocument) -> CheckResult:
    if document.total_pages is None:
        return CheckResult(
            id="page_count",
            category="layout",
            severity="low",
            status="not_applicable",
            params={"reason": document.file_format},
        )
    return CheckResult(
        id="page_count",
        category="layout",
        severity="low",
        status="fail" if document.total_pages > MAX_RECOMMENDED_PAGES else "pass",
        params={"pages": document.total_pages, "max_pages": MAX_RECOMMENDED_PAGES},
    )


def has_text_layer(document: ExtractedDocument) -> bool:
    """Whether any readable text (beyond unmapped-glyph placeholders) exists."""
    return bool(_CID_RE.sub("", document.text).strip())


def run_layout_checks(
    document: ExtractedDocument, deadline: float | None = None
) -> list[CheckResult]:
    """Run every extraction and layout check in a fixed order."""
    text_layer = has_text_layer(document)
    checks = [
        CheckResult(
            id="text_layer",
            category="extraction",
            severity="fatal",
            status="pass" if text_layer else "fail",
            params={"chars": len(document.text)},
        ),
        CheckResult(
            id="truncated",
            category="extraction",
            severity="medium",
            status="fail"
            if document.truncated_pages
            or document.truncated_chars
            or document.dense_pages
            else "pass",
            params={
                "pages_truncated": document.truncated_pages,
                "chars_truncated": document.truncated_chars,
                "dense_pages": list(document.dense_pages),
                "page_limit": MAX_ANALYZED_PAGES,
                "char_limit": MAX_EXTRACTED_CHARS,
                "page_char_limit": MAX_PAGE_CHARS,
                "page_line_limit": MAX_LINES_PER_PAGE,
            },
        ),
        *_glyph_checks(document.text),
        *_column_checks(document, deadline),
        _table_check(document),
        *_image_checks(document),
        _text_box_check(document),
        _header_footer_contact(document),
        _page_count_check(document),
    ]
    return checks
