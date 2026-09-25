"""Parse checks on real template renders (committed fixtures, no Chromium needed).

``tests/fixtures/ats_parse/renders/`` holds PDFs produced by the real download
route for all seven templates, plus the ``processed_resume`` payload they
rendered (see ``tests/ats_parse_renders.py``). These tests pin the column
detector and the round trip on what users actually download. The renders come
from the production Docker image (Linux); the few assertions that depend on
the renderer's fonts key on the manifest's ``platform``.
"""

import json
import re
from functools import cache
from pathlib import Path
from typing import Any, get_args

import pytest

from app.services.ats_parse import check_document_sync, report_to_json
from app.services.ats_parse.extract import extract_document
from app.services.ats_parse.messages_en import EXPECTED_BY_TEMPLATE_MESSAGE, render_message
from app.services.ats_parse.report import ParseCheckReport
from app.services.ats_parse.templates import (
    LOCALIZED_DEFAULT_HEADINGS,
    TEMPLATE_IDS,
    TEMPLATE_LAYOUTS,
    RenderLocale,
    localize_section_meta,
)
from tests.ats_parse_renders import manifest, render_pdf, render_stems, source

FRONTEND = Path(__file__).resolve().parents[4] / "apps" / "frontend"
SINGLE_COLUMN = ("swiss-single", "modern", "latex", "clean")
TWO_COLUMN = ("swiss-two-column", "modern-two-column", "vivid")
CONTACT_FIELDS = ("name", "email", "phone", "location", "website", "linkedin", "github")


@cache
def _source() -> dict[str, Any]:
    return source()


@cache
def _report(stem: str) -> ParseCheckReport:
    settings = manifest()["files"][stem]
    return check_document_sync(
        render_pdf(stem),
        "render.pdf",
        content_language="en",
        render_locale=settings["lang"] or "en",
        template=settings["template"],
        roundtrip_source=_source(),
    )


@cache
def _text(stem: str) -> str:
    return extract_document(render_pdf(stem), "render.pdf").text


def _checks(stem: str) -> dict[str, Any]:
    return {check.id: check for check in _report(stem).checks}


def _fields(stem: str) -> dict[str, str]:
    roundtrip = _report(stem).roundtrip
    assert roundtrip is not None
    return {field.field: field.status for field in roundtrip.fields}


def test_every_template_has_a_committed_render() -> None:
    files = manifest()["files"]
    assert {files[stem]["template"] for stem in files} == set(TEMPLATE_IDS)
    assert render_stems() == set(files)


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
    # The bullet is scored inside its own entry; in a two-column layout the
    # sidebar may interleave with its wrapped line (garbled, not missing).
    allowed = {"found", "garbled"} if TEMPLATE_LAYOUTS[template].two_column else {"found"}
    assert fields["workExperience[0].description[2]"] in allowed


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


@pytest.mark.parametrize("template", TEMPLATE_IDS)
def test_visible_custom_item_section_is_found(template: str) -> None:
    fields = _fields(template)
    publications = {path: status for path, status in fields.items() if "publications" in path}
    if manifest()["platform"] == "darwin" and template in ("clean", "vivid"):
        # The subtitle is small caps: U+F765 on macOS (see the small-caps test).
        publications.pop("customSections.publications[0].subtitle")
    assert set(publications.values()) == {"found"}
    assert "customSections.publications[0].description[0]" in publications
    assert fields["heading.volunteering"] == "hidden"


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


def test_contact_icons_are_inline_svg_components() -> None:
    """``showContactIcons`` draws lucide-react icons, i.e. inline SVG paths,
    which extract no text: they cannot trip ``icon_font_glyphs`` the way an
    icon font's Private Use Area glyphs do (``icon_font.pdf``)."""
    components = [
        path
        for path in sorted((FRONTEND / "components" / "resume").glob("resume-*.tsx"))
        if "showContactIcons" in path.read_text()
    ]
    assert len(components) == len(TEMPLATE_IDS)
    for path in components:
        source = path.read_text()
        imported = re.search(r"import \{([^}]*)\} from 'lucide-react';", source)
        assert imported is not None, path.name
        lucide = {name.strip() for name in imported.group(1).split(",")}
        block = source.split("const contactIcons", 1)[1].split("= {", 1)[1].split("};", 1)[0]
        icons = set(re.findall(r"<(\w+)", block))
        assert icons, path.name
        assert icons <= lucide, path.name
        assert "className" not in block, path.name  # no icon-font class names


@pytest.mark.parametrize("template", ("clean", "vivid"))
def test_small_caps_titles_depend_on_the_renderer_font(template: str) -> None:
    """clean and vivid set job titles in ``font-variant: small-caps``.

    With the macOS system font Chromium uses the font's small-cap glyphs and
    emits "e" as U+F765 (Private Use Area), so titles are lost. The Linux
    image's fonts (DejaVu) have no small caps; Chromium synthesizes them from
    capitals, which extract as plain uppercase text.
    """
    icon_glyphs = _checks(template)["icon_font_glyphs"]
    title = _fields(template)["workExperience[0].title"]
    if manifest()["platform"] == "darwin":
        assert icon_glyphs.status == "fail"
        assert icon_glyphs.evidence["codepoints"] == ["U+F765"]
        assert title == "missing"
    else:
        assert icon_glyphs.status == "pass"
        assert "SENIOR SOFTWARE ENGINEER" in _text(template)
        assert title == "found"


def test_letter_spaced_headings_are_read_as_words() -> None:
    """clean's section headings use ``letter-spacing: 0.12em``; the uniform
    gaps are the line's typical gap, not word breaks."""
    text = _text("clean")
    for heading in ("SUMMARY", "EXPERIENCE", "EDUCATION", "PUBLICATIONS"):
        assert heading in text
    fields = _fields("clean")
    for key in ("summary", "workExperience", "education", "personalProjects", "publications"):
        assert fields[f"heading.{key}"] == "found"
    assert _checks("clean")["section_headings"].status == "pass"


@pytest.mark.parametrize(
    ("template", "words"),
    [
        ("vivid", ("jordan.rivera@example.com", "EDUCATION", "LANGUAGES", "PUBLICATIONS")),
        ("swiss-two-column", ("EDUCATION", "PostgreSQL")),
        ("modern-two-column", ("EDUCATION", "PostgreSQL")),
    ],
)
def test_kerning_gaps_do_not_split_words(template: str, words: tuple[str, ...]) -> None:
    """Tightly kerned capitals and vivid's header (wide kerning gaps with the
    Linux image's fonts) stay whole, as pdftotext reads them."""
    text = _text(template)
    for word in words:
        assert word in text
    assert _fields(template)["personalInfo.email"] == "found"


def test_word_gap_without_a_space_glyph_is_a_break() -> None:
    """The two-column education line draws " | " as its own span with no
    space glyph; that gap still separates the words."""
    assert "University | Aug 2011" in _text("swiss-two-column")


def test_real_render_report_is_byte_identical_across_runs() -> None:
    settings = manifest()["files"]["vivid"]
    content = render_pdf("vivid")
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


def _frontend_locales() -> tuple[str, ...]:
    config = (FRONTEND / "i18n" / "config.ts").read_text()
    block = config.split("export const locales =", 1)[1].split("]", 1)[0]
    return tuple(re.findall(r"'([A-Za-z-]+)'", block))


def _locale_message_files() -> dict[str, str]:
    """Locale -> message file, as ``lib/i18n/messages.ts`` imports them."""
    loader = (FRONTEND / "lib" / "i18n" / "messages.ts").read_text()
    return dict(re.findall(r"import (\w+) from '@/messages/([\w-]+\.json)';", loader))


def test_render_locales_are_the_frontend_locales() -> None:
    assert set(get_args(RenderLocale)) == set(_frontend_locales())
    assert set(LOCALIZED_DEFAULT_HEADINGS) == set(_frontend_locales())
    assert _locale_message_files()["pt"] == "pt-BR.json"


@pytest.mark.parametrize("locale", sorted(LOCALIZED_DEFAULT_HEADINGS))
def test_localized_default_headings_match_the_locale_files(locale: str) -> None:
    message_file = _locale_message_files()[locale]
    messages = json.loads((FRONTEND / "messages" / message_file).read_text())
    sections = messages["resume"]["sections"]
    assert LOCALIZED_DEFAULT_HEADINGS[locale] == {
        "summary": sections["summary"],
        "workExperience": sections["experience"],
        "education": sections["education"],
        "personalProjects": sections["projects"],
        "additional": sections["skills"],
    }


def test_portuguese_render_locale_localizes_headings_from_pt_br_file() -> None:
    sections = json.loads((FRONTEND / "messages" / "pt-BR.json").read_text())["resume"]["sections"]
    localized = localize_section_meta(_source(), "pt")
    names = {entry["key"]: entry["displayName"] for entry in localized["sectionMeta"]}
    assert names["workExperience"] == sections["experience"]
    assert names["education"] == sections["education"]
    assert names["publications"] == "Publications"  # custom sections keep their name


def test_expected_by_template_is_rendered_in_the_english_message() -> None:
    message = render_message(_checks("swiss-two-column")["multi_column"])
    assert message.endswith(EXPECTED_BY_TEMPLATE_MESSAGE)
    assert EXPECTED_BY_TEMPLATE_MESSAGE not in render_message(_checks("swiss-single")["multi_column"])
