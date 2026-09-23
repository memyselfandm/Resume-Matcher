"""ATS parse-check engine tests against the committed synthetic fixtures.

Fixtures live in ``tests/fixtures/ats_parse`` and are produced by
``scripts/generate_ats_parse_fixtures.py``; no Chromium is needed here.
"""

from pathlib import Path

import pytest

from app.services.ats_parse import check_document_sync, report_to_json
from app.services.ats_parse import extract as extract_module
from app.services.ats_parse.extract import extract_document
from app.services.ats_parse.messages_en import render_message
from app.services.ats_parse.report import CheckResult, ParseCheckReport
from app.services.parser import DocumentResourceLimitError, DocumentValidationError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ats_parse"


def _report(name: str, **kwargs: str) -> ParseCheckReport:
    return check_document_sync((FIXTURES / name).read_bytes(), name, **kwargs)


def _check(report: ParseCheckReport, check_id: str) -> CheckResult:
    matches = [check for check in report.checks if check.id == check_id]
    assert len(matches) == 1, f"expected exactly one {check_id} check"
    return matches[0]


class TestExtractability:
    def test_image_only_pdf_has_no_text_layer_and_fatal_score(self) -> None:
        report = _report("image_only.pdf")
        assert report.extractability == "none"
        text_layer = _check(report, "text_layer")
        assert text_layer.status == "fail"
        assert text_layer.severity == "fatal"
        assert report.overall_score is not None and report.overall_score <= 10
        assert _check(report, "text_as_image").status == "fail"
        assert all(profile.passes is False for profile in report.profiles)
        content = [check for check in report.checks if check.category == "content"]
        assert content and all(check.status == "not_applicable" for check in content)

    def test_clean_single_column_pdf_passes_every_check(self) -> None:
        report = _report("clean_single_column.pdf")
        assert report.extractability == "full"
        assert report.content_language == "en"
        assert [check.id for check in report.checks if check.status == "fail"] == []
        assert report.overall_score == 100
        assert all(profile.passes for profile in report.profiles)

    def test_legacy_doc_is_unsupported_format(self) -> None:
        report = _report("legacy.doc")
        assert report.extractability == "unsupported_format"
        assert report.overall_score is None
        assert [(check.id, check.status) for check in report.checks] == [
            ("file_format", "fail")
        ]
        assert "PDF or DOCX" in render_message(report.checks[0])

    def test_decompression_bomb_is_rejected_by_bounded_parser(self) -> None:
        with pytest.raises(DocumentResourceLimitError):
            _report("decompression_bomb.pdf")

    def test_corrupt_pdf_is_a_validation_error(self) -> None:
        with pytest.raises(DocumentValidationError):
            check_document_sync(b"%PDF-1.4 not really a pdf", "broken.pdf")

    def test_unknown_suffix_is_a_validation_error(self) -> None:
        with pytest.raises(DocumentValidationError):
            check_document_sync(b"plain text", "resume.txt")


class TestColumnDetection:
    def test_swiss_single_right_aligned_dates_pass_multi_column(self) -> None:
        report = _report("swiss_single_like.pdf")
        assert _check(report, "multi_column").status == "pass"
        assert _check(report, "sidebar").status == "pass"
        # Row reconstruction keeps the right-aligned date on its title row.
        assert "Senior Software Engineer Jan 2021 - Present" in report.extracted_text_preview

    def test_swiss_two_column_geometry_fails_multi_column_once(self) -> None:
        report = _report("two_column.pdf")
        multi_column = _check(report, "multi_column")
        assert multi_column.status == "fail"
        assert multi_column.params == {"pages": [1]}
        evidence = multi_column.evidence["pages"][0]
        assert evidence["height_ratio"] >= 0.4
        # The band sits between the 65% main column and the 35% sidebar.
        assert 370 <= evidence["gutter_x0"] <= 392
        # The narrow right column is the same defect: reported once, not twice.
        sidebar = _check(report, "sidebar")
        assert sidebar.status == "not_applicable"
        assert sidebar.params == {
            "pages": [],
            "suppressed_pages": [1],
            "reason": "covered_by_multi_column",
        }

    def test_balanced_two_column_is_multi_column_but_not_sidebar(self) -> None:
        report = _report("balanced_two_column.pdf")
        assert _check(report, "multi_column").status == "fail"
        assert _check(report, "sidebar").status == "pass"

    def test_docx_section_columns_fail_multi_column(self) -> None:
        report = _report("two_column_section.docx")
        check = _check(report, "multi_column")
        assert check.status == "fail"
        assert check.params == {"section_columns": 2}
        assert _check(_report("clean.docx"), "multi_column").status == "pass"


class TestGlyphChecks:
    def test_icon_font_glyphs_fail_with_codepoint_evidence(self) -> None:
        report = _report("icon_font.pdf")
        check = _check(report, "icon_font_glyphs")
        assert check.status == "fail"
        assert check.params["count"] == 3
        assert check.evidence["codepoints"] == ["U+F08C", "U+F095", "U+F0E0"]
        assert _check(report, "unmapped_glyphs").status == "pass"

    def test_unmapped_cid_glyphs_fail_with_cid_evidence(self) -> None:
        report = _report("cid_glyphs.pdf")
        check = _check(report, "unmapped_glyphs")
        assert check.status == "fail"
        assert check.params["count"] > 100
        assert check.evidence["cids"] and all(isinstance(cid, int) for cid in check.evidence["cids"])
        assert report.extractability == "partial"
        assert _check(report, "icon_font_glyphs").status == "pass"


class TestDocxChecks:
    def test_table_layout_docx_fails_tables(self) -> None:
        report = _report("table_layout.docx")
        assert _check(report, "tables").status == "fail"
        assert _check(report, "tables").params == {"count": 1}
        # Cell line breaks survive as whitespace, so cell text stays readable.
        assert "Senior Software Engineer\nNorthwind Analytics" in report.extracted_text_preview

    def test_contact_only_in_header_fails(self) -> None:
        report = _report("contact_in_header.docx")
        check = _check(report, "header_footer_contact")
        assert check.status == "fail"
        assert check.params == {"fields": ["email", "phone"]}
        assert _check(report, "contact_email").status == "fail"

    def test_header_phone_is_reported_despite_body_year_ranges(self) -> None:
        text = _report("contact_in_header.docx").extracted_text_preview
        document = extract_document((FIXTURES / "contact_in_header.docx").read_bytes(), "x.docx")
        assert "2015 - 2019 2019 - 2021" in document.text
        assert _check(_report("contact_in_header.docx"), "contact_phone").status == "fail"
        assert "(555)" not in text

    def test_text_box_content_is_reported_once_and_not_in_body(self) -> None:
        report = _report("text_box.docx")
        check = _check(report, "text_boxes")
        assert check.status == "fail"
        assert check.params == {"chars": len("Certified Kubernetes Administrator, 2022")}
        assert "Kubernetes Administrator" not in report.extracted_text_preview
        assert _check(_report("clean.docx"), "text_boxes").status == "pass"

    def test_clean_docx_contact_in_body_passes(self) -> None:
        report = _report("clean.docx")
        assert _check(report, "header_footer_contact").status == "pass"
        assert _check(report, "tables").status == "pass"
        assert _check(report, "page_count").status == "not_applicable"


class TestLanguageGating:
    def test_spanish_resume_gates_english_lexicon_checks(self) -> None:
        report = _report("spanish.pdf")
        assert report.content_language == "es"
        assert _check(report, "action_verbs").status == "not_applicable"
        assert _check(report, "month_year_dates").status == "not_applicable"
        # Diacritics are ordinary letters, not unreadable characters.
        assert _check(report, "replacement_characters").status == "pass"
        assert _check(report, "icon_font_glyphs").status == "pass"
        assert _check(report, "section_headings").status == "pass"

    def test_explicit_content_language_overrides_detection(self) -> None:
        report = _report("clean_single_column.pdf", content_language="fr")
        assert report.content_language == "fr"
        assert _check(report, "action_verbs").status == "not_applicable"

    def test_english_resume_with_spanish_render_locale_expects_spanish_headings(
        self,
    ) -> None:
        report = _report("clean_single_column.pdf", render_locale="es")
        assert _check(report, "action_verbs").status == "pass"
        headings = _check(report, "section_headings")
        assert headings.status == "fail"
        assert headings.params["render_locale"] == "es"
        assert headings.params["missing"] == ["experience", "education", "skills"]
        spanish = _report("spanish.pdf", render_locale="es")
        assert _check(spanish, "section_headings").status == "pass"


class TestCaps:
    def test_pdf_over_page_limit_is_truncated_not_timed_out(self) -> None:
        report = _report("twelve_pages.pdf")
        check = _check(report, "truncated")
        assert check.status == "fail"
        assert check.params["pages_truncated"] is True
        assert check.params["page_limit"] == 10
        assert _check(report, "page_count").params["pages"] == 12
        assert report.extractability == "partial"
        assert "Project Log 10" in extract_document(
            (FIXTURES / "twelve_pages.pdf").read_bytes(), "x.pdf"
        ).text
        assert "Project Log 11" not in extract_document(
            (FIXTURES / "twelve_pages.pdf").read_bytes(), "x.pdf"
        ).text

    def test_extracted_character_cap_truncates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(extract_module, "MAX_EXTRACTED_CHARS", 500)
        document = extract_document(
            (FIXTURES / "clean_single_column.pdf").read_bytes(), "x.pdf"
        )
        assert document.truncated_chars is True
        assert len(document.text.replace("\n", "")) <= 500


class TestDeterminism:
    @pytest.mark.parametrize(
        "name",
        ["swiss_single_like.pdf", "two_column.pdf", "table_layout.docx", "spanish.pdf"],
    )
    def test_same_input_serializes_byte_identically(self, name: str) -> None:
        first = report_to_json(_report(name))
        second = report_to_json(_report(name))
        assert first == second
        assert first.startswith('{"checks":')

    def test_report_carries_schema_version_and_ids_without_prose(self) -> None:
        report = _report("two_column.pdf")
        assert report.schema_version == "1.0"
        for check in report.checks:
            assert set(check.model_dump()) == {
                "id",
                "category",
                "severity",
                "status",
                "params",
                "evidence",
            }


def test_every_emitted_check_renders_an_english_message() -> None:
    names = sorted(
        path.name
        for path in FIXTURES.iterdir()
        if path.suffix in {".pdf", ".docx", ".doc"} and path.name != "decompression_bomb.pdf"
    )
    seen: set[str] = set()
    for name in names:
        for check in _report(name).checks:
            message = render_message(check)
            assert message and "{" not in message
            seen.add(check.id)
    assert {"multi_column", "sidebar", "text_layer", "unmapped_glyphs"} <= seen
