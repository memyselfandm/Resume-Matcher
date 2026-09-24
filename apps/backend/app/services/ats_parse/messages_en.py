"""English messages for parse-check results (MCP and CLI consumers only).

The web UI renders check ids from its locale files; this catalog exists so
agent-facing summaries are readable without the frontend. Every check id the
engine can emit has a failure message and a pass message.
"""

from __future__ import annotations

from typing import Any

from app.services.ats_parse.report import CheckResult

FAIL_MESSAGES: dict[str, str] = {
    "file_format": (
        "Legacy .doc files are not analyzed. Save the resume as PDF or DOCX and "
        "check it again."
    ),
    "text_layer": (
        "No selectable text was found. An ATS sees an empty resume; export a "
        "text-based PDF or run OCR."
    ),
    "truncated": (
        "Part of the document was not analyzed (limits: {page_limit} pages, "
        "{char_limit} characters; pages skipped as too dense: {dense_pages})."
    ),
    "unmapped_glyphs": (
        "{count} glyphs extracted as (cid:NN) placeholders. The PDF font has no "
        "Unicode mapping, so an ATS reads garbage for that text."
    ),
    "icon_font_glyphs": (
        "{count} icon-font characters extracted from the Private Use Area. "
        "Replace icons with plain-text labels."
    ),
    "replacement_characters": (
        "{count} unreadable replacement or control characters extracted."
    ),
    "text_as_image": (
        "Page(s) {pages} are mostly images with little text; the content is "
        "probably invisible to an ATS."
    ),
    "multi_column": (
        "Multi-column layout detected. An ATS may read the columns out of order."
    ),
    "sidebar": (
        "Narrow sidebar detected on page(s) {pages}. Sidebar content is often "
        "merged into the main text or skipped."
    ),
    "tables": "Tables detected. Content inside tables is often skipped or scrambled.",
    "images": "Images detected. An ATS cannot read text inside images.",
    "text_boxes": (
        "Text boxes contain {chars} characters that many ATS parsers skip."
    ),
    "header_footer_contact": (
        "Contact details ({fields}) appear only in the page header or footer, "
        "which many ATS parsers ignore."
    ),
    "page_count": "The resume is {pages} pages; most systems prefer {max_pages} or fewer.",
    "contact_email": "No email address was found in the extracted text.",
    "contact_phone": "No phone number was found in the extracted text.",
    "contact_linkedin": "No LinkedIn profile URL was found in the extracted text.",
    "section_headings": "Standard section headings not found: {missing}.",
    "dates_present": "Only {count} year mentions found; add dates to each position.",
    "month_year_dates": "Dates have no month; use formats like 'Jan 2021' or '01/2021'.",
    "action_verbs": (
        "{count} distinct action verbs found; aim for {min_count} or more."
    ),
    "quantification": (
        "{count} lines contain measurable results; aim for {min_count} or more."
    ),
    "length": "The resume has {words} words ({verdict}); aim for {min_words}-{max_words}.",
}

PASS_MESSAGES: dict[str, str] = {
    "file_format": "The file format is supported.",
    "text_layer": "Selectable text was found.",
    "truncated": "The whole document was analyzed.",
    "unmapped_glyphs": "All glyphs map to readable characters.",
    "icon_font_glyphs": "No icon-font characters were extracted.",
    "replacement_characters": "No unreadable characters were extracted.",
    "text_as_image": "No page is text rendered as an image.",
    "multi_column": "Single-column reading order.",
    "sidebar": "No sidebar detected.",
    "tables": "No tables detected.",
    "images": "No images detected.",
    "text_boxes": "No text boxes detected.",
    "header_footer_contact": "Contact details are in the document body.",
    "page_count": "Page count is within the recommended range.",
    "contact_email": "Email address found.",
    "contact_phone": "Phone number found.",
    "contact_linkedin": "LinkedIn profile found.",
    "section_headings": "Standard section headings found.",
    "dates_present": "Dates found.",
    "month_year_dates": "Dates include months.",
    "action_verbs": "Bullets use a variety of action verbs.",
    "quantification": "Results are quantified.",
    "length": "Length is within the recommended range.",
}

NOT_APPLICABLE_MESSAGE = "Not applicable ({reason})."


def _format_value(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value) or "none"
    return str(value)


def render_message(check: CheckResult) -> str:
    """Render one check result as an English sentence."""
    params = {key: _format_value(value) for key, value in check.params.items()}
    if check.status == "not_applicable":
        return NOT_APPLICABLE_MESSAGE.format(reason=params.get("reason", "n/a"))
    catalog = FAIL_MESSAGES if check.status == "fail" else PASS_MESSAGES
    return catalog[check.id].format(**params)
