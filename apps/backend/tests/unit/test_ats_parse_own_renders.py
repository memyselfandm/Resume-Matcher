"""Parse checks on real template renders (committed fixtures, no Chromium needed).

``tests/fixtures/ats_parse/renders/`` holds PDFs produced by the real download
route for all seven templates, plus the ``processed_resume`` payload they
rendered (``scripts/generate_ats_render_fixtures.py``). These tests pin the
column detector and the round-trip on what users actually download.
"""

import json
import re
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from app.services.ats_parse import check_document_sync, report_to_json
from app.services.ats_parse.extract import extract_document
from app.services.ats_parse.messages_en import EXPECTED_BY_TEMPLATE_MESSAGE, render_message
from app.services.ats_parse.report import ParseCheckReport
from app.services.ats_parse.templates import (
    LOCALIZED_DEFAULT_HEADINGS,
    TEMPLATE_IDS,
    TEMPLATE_LAYOUTS,
)

RENDERS = Path(__file__).resolve().parents[1] / "fixtures" / "ats_parse" / "renders"
FRONTEND = Path(__file__).resolve().parents[4] / "apps" / "frontend"
SINGLE_COLUMN = ("swiss-single", "modern", "latex", "clean")
TWO_COLUMN = ("swiss-two-column", "modern-two-column", "vivid")
CONTACT_FIELDS = ("name", "email", "phone", "location", "website", "linkedin", "github")


@cache
def _source() -> dict[str, Any]:
    return json.loads((RENDERS / "source.json").read_text())


@cache
def _manifest() -> dict[str, Any]:
    return json.loads((RENDERS / "manifest.json").read_text())


@cache
def _report(stem: str) -> ParseCheckReport:
    settings = _manifest()["files"][stem]
    return check_document_sync(
        (RENDERS / f"{stem}.pdf").read_bytes(),
        "render.pdf",
        content_language="en",
        render_locale=settings["lang"] or "en",
        template=settings["template"],
        roundtrip_source=_source(),
    )


@cache
def _text(stem: str) -> str:
    return extract_document((RENDERS / f"{stem}.pdf").read_bytes(), "render.pdf").text


def _checks(stem: str) -> dict[str, Any]:
    return {check.id: check for check in _report(stem).checks}


def _fields(stem: str) -> dict[str, str]:
    roundtrip = _report(stem).roundtrip
    assert roundtrip is not None
    return {field.field: field.status for field in roundtrip.fields}


def test_every_template_has_a_committed_render() -> None:
    files = _manifest()["files"]
    assert {files[stem]["template"] for stem in files} == set(TEMPLATE_IDS)
    for stem in files:
        assert (RENDERS / f"{stem}.pdf").is_file()


@pytest.mark.parametrize("template", SINGLE_COLUMN)
def test_single_column_templates_pass_column_checks(template: str) -> None:
    checks = _checks(template)
    assert checks["multi_column"].status == "pass"
    assert checks["sidebar"].status == "pass"
    assert "expected_by_template" not in checks["multi_column"].params
    assert TEMPLATE_LAYOUTS[template].two_column is False


@pytest.mark.parametrize("template", TWO_COLUMN)
def test_two_column_templates_fail_multi_column_as_expected(template: str) -> None:
    multi_column = _checks(template)["multi_column"]
    assert multi_column.status == "fail"
    assert multi_column.params["expected_by_template"] is True
    assert multi_column.severity == "medium"
    assert TEMPLATE_LAYOUTS[template].two_column is True
    # The gutter runs the full height of the two-column body.
    assert multi_column.evidence["pages"][0]["height_ratio"] >= 0.8


@pytest.mark.parametrize("template", ("swiss-single", "modern", "latex"))
def test_clean_templates_round_trip(template: str) -> None:
    roundtrip = _report(template).roundtrip
    assert roundtrip is not None
    assert roundtrip.content_recall >= 0.95
    assert roundtrip.order_fidelity >= 0.95
    fields = _fields(template)
    for name in CONTACT_FIELDS:
        assert fields[f"personalInfo.{name}"] == "found"


@pytest.mark.parametrize("template", ("swiss-single", "latex", "swiss-two-column"))
def test_repeated_employer_entries_anchor_to_their_own_text(template: str) -> None:
    """Both jobs are at Northwind Analytics, and a bullet of the first names it."""
    fields = _fields(template)
    for index in (0, 1):
        assert fields[f"workExperience[{index}].company"] == "found"
        assert fields[f"workExperience[{index}].years"] == "found"
    assert fields["workExperience[0].description[2]"] == "found"


def test_uppercase_rendered_headings_are_found() -> None:
    text = _text("swiss-single")
    assert "EXPERIENCE" in text and "Experience" not in text
    fields = _fields("swiss-single")
    for key in ("summary", "workExperience", "education", "publications"):
        assert fields[f"heading.{key}"] == "found"


@pytest.mark.parametrize("template", TEMPLATE_IDS)
def test_hidden_section_is_hidden_not_missing(template: str) -> None:
    fields = _fields(template)
    assert fields["customSections.volunteering.strings[0]"] == "hidden"
    assert "Youth Coding Club" not in _text(template)


@pytest.mark.parametrize("template", TWO_COLUMN)
def test_two_column_additional_heading_is_not_rendered(template: str) -> None:
    """Two-column templates print fixed per-list headings instead."""
    assert _fields(template)["heading.additional"] == "not_rendered"
    assert "missing" not in {
        status for path, status in _fields(template).items() if path.startswith("additional.")
    }


def test_spanish_render_locale_keeps_english_checks_and_spanish_headings() -> None:
    report = _report("swiss-single-es")
    checks = _checks("swiss-single-es")
    assert report.content_language == "en"
    assert checks["action_verbs"].status != "not_applicable"
    assert checks["section_headings"].status == "pass"
    assert checks["section_headings"].params["render_locale"] == "es"
    assert "EXPERIENCIA" in _text("swiss-single-es")
    fields = _fields("swiss-single-es")
    assert fields["heading.workExperience"] == "found"
    assert fields["heading.education"] == "found"


def test_contact_icons_are_vector_drawings_not_icon_font_glyphs() -> None:
    """The templates draw contact icons as inline SVG, which extracts no text."""
    icons = _checks("swiss-single-icons")
    assert icons["icon_font_glyphs"].status == "pass"
    assert _report("swiss-single-icons").roundtrip.content_recall == 1.0  # type: ignore[union-attr]


def test_small_caps_private_use_glyphs_are_reported() -> None:
    """The clean template's small-caps job titles, rendered with the macOS system
    font (see ``manifest.json``), extract "e" as U+F765: a real parse defect."""
    assert _manifest()["platform"] == "darwin"
    icon_glyphs = _checks("clean")["icon_font_glyphs"]
    assert icon_glyphs.status == "fail"
    assert icon_glyphs.evidence["codepoints"] == ["U+F765"]
    assert _fields("clean")["workExperience[0].title"] != "found"


def test_real_render_report_is_byte_identical_across_runs() -> None:
    settings = _manifest()["files"]["vivid"]
    content = (RENDERS / "vivid.pdf").read_bytes()
    runs = [
        report_to_json(
            check_document_sync(
                content,
                "render.pdf",
                content_language="en",
                template=settings["template"],
                roundtrip_source=_source(),
            )
        )
        for _ in range(2)
    ]
    assert runs[0] == runs[1]


def test_template_ids_match_the_frontend() -> None:
    source = (FRONTEND / "lib" / "types" / "template-settings.ts").read_text()
    block = source.split("export type TemplateType =", 1)[1].split(";", 1)[0]
    assert tuple(re.findall(r"'([a-z-]+)'", block)) == TEMPLATE_IDS


@pytest.mark.parametrize("locale", sorted(LOCALIZED_DEFAULT_HEADINGS))
def test_localized_default_headings_match_the_locale_files(locale: str) -> None:
    messages = json.loads((FRONTEND / "messages" / f"{locale}.json").read_text())
    sections = messages["resume"]["sections"]
    assert LOCALIZED_DEFAULT_HEADINGS[locale] == {
        "summary": sections["summary"],
        "workExperience": sections["experience"],
        "education": sections["education"],
        "personalProjects": sections["projects"],
        "additional": sections["skills"],
    }


def test_every_frontend_locale_has_localized_headings() -> None:
    locales = {path.stem for path in (FRONTEND / "messages").glob("*.json")}
    assert locales == set(LOCALIZED_DEFAULT_HEADINGS)


def test_expected_by_template_is_rendered_in_the_english_message() -> None:
    message = render_message(_checks("swiss-two-column")["multi_column"])
    assert message.endswith(EXPECTED_BY_TEMPLATE_MESSAGE)
    assert EXPECTED_BY_TEMPLATE_MESSAGE not in render_message(_checks("swiss-single")["multi_column"])
