"""Unit tests for parse-check rules on synthetic inputs (no fixture files)."""

import itertools

import pytest

from app.services.ats_parse.content_checks import (
    detect_content_language,
    find_section_headings,
    run_content_checks,
)
from app.services.ats_parse.extract import (
    ExtractedDocument,
    PageLayout,
    TextLine,
    reconstruct_rows,
)
from app.services.ats_parse.layout_checks import find_gutters, run_layout_checks
from app.services.ats_parse.messages_en import FAIL_MESSAGES, PASS_MESSAGES
from app.services.ats_parse.normalize import normalize_text, tokenize
from app.services.ats_parse.profiles import PROFILES, score_profiles
from app.services.ats_parse.report import CheckResult, overall_score

LINE_HEIGHT = 9.0
LINE_STEP = 11.0


def _page(lines: list[TextLine]) -> PageLayout:
    return PageLayout(
        number=1, width=612.0, height=792.0, lines=tuple(lines), image_sizes=(), char_count=500
    )


def _stack(x0: float, widths: list[float], top: float = 700.0) -> list[TextLine]:
    return [
        TextLine(x0, top - i * LINE_STEP - LINE_HEIGHT, x0 + width, top - i * LINE_STEP, f"l{i}")
        for i, width in enumerate(widths)
    ]


class TestGutterDetector:
    def test_true_columns_produce_a_tall_gutter(self) -> None:
        widths = [250.0, 230.0, 240.0, 210.0] * 6
        left = _stack(30.0, widths)
        right = _stack(400.0, [150.0, 120.0, 170.0, 90.0] * 6)
        candidates = find_gutters(_page(left + right))
        assert candidates
        assert any(280.0 <= c.x0 and c.x1 <= 400.0 for c in candidates)

    def test_equal_width_right_aligned_dates_are_not_a_column(self) -> None:
        """Same-width dates share both edges: a stack, not a ragged column."""
        left = _stack(30.0, [200.0, 150.0, 180.0, 120.0] * 5)
        dates = [
            TextLine(520.0, 700.0 - i * LINE_STEP - LINE_HEIGHT, 582.0, 700.0 - i * LINE_STEP, "2019 - 2021")
            for i in range(20)
        ]
        assert find_gutters(_page(left + dates)) == []

    def test_full_width_lines_interrupt_the_band(self) -> None:
        lines: list[TextLine] = []
        top = 700.0
        for _ in range(4):
            lines += _stack(30.0, [200.0, 150.0, 180.0], top)
            lines += _stack(400.0, [150.0, 120.0, 170.0], top)
            full = top - 3 * LINE_STEP
            lines.append(TextLine(30.0, full - LINE_HEIGHT, 582.0, full, "full width"))
            top = full - LINE_STEP
        candidates = find_gutters(_page(lines))
        text_height = max(l.y1 for l in lines) - min(l.y0 for l in lines)
        assert candidates
        assert all((c.run_top - c.run_bottom) / text_height < 0.4 for c in candidates)
        document = ExtractedDocument(
            file_format="pdf", text="x", pages=(_page(lines),), total_pages=1
        )
        checks = {check.id: check for check in run_layout_checks(document)}
        # Short side-by-side blocks are not a page-level column layout...
        assert checks["multi_column"].status == "pass"
        # ...but a narrow block beside main content is still a sidebar.
        assert checks["sidebar"].status == "fail"

    def test_too_few_lines_never_detect(self) -> None:
        assert find_gutters(_page(_stack(30.0, [100.0, 80.0]) + _stack(400.0, [90.0]))) == []


def test_rows_merge_same_baseline_and_order_top_down() -> None:
    lines = [
        TextLine(520.0, 690.0, 582.0, 699.0, "Jan 2021 - Present"),
        TextLine(30.0, 690.5, 150.0, 699.5, "Senior Engineer"),
        TextLine(30.0, 670.0, 150.0, 679.0, "Northwind"),
    ]
    assert reconstruct_rows(lines) == ["Senior Engineer Jan 2021 - Present", "Northwind"]


class TestNormalization:
    def test_ligatures_case_dashes_quotes_and_html(self) -> None:
        raw = "<b>Certi\ufb01cate</b> \u2013 \u201cData\u201d &amp; ML"
        assert normalize_text(raw, source_html=True) == 'certificate - "data" & ml'

    def test_line_wrap_hyphenation_is_joined(self) -> None:
        assert normalize_text("develop-\nment") == "development"

    def test_tokens_keep_technical_names(self) -> None:
        assert tokenize("c++ node.js c# ci/cd") == ["c++", "node.js", "c#", "ci/cd"]

    def test_links_are_single_tokens_without_scheme(self) -> None:
        assert tokenize("https://www.linkedin.com/in/jordan-rivera/ | a@b.io") == [
            "linkedin.com/in/jordan-rivera",
            "a@b.io",
        ]


class TestLanguageDetection:
    def test_english_text(self) -> None:
        text = (
            "Led the migration of the platform to a new stack and improved the "
            "reliability of the service for the team. Built tools for the analysts "
            "in the company and worked with the product group on the roadmap."
        )
        assert detect_content_language(text) == "en"

    def test_short_text_is_unknown(self) -> None:
        assert detect_content_language("Python Go Kafka") == "unknown"

    def test_cjk_scripts(self) -> None:
        assert detect_content_language("山田太郎 ソフトウェアエンジニア 経歴 東京") == "ja"
        assert detect_content_language("김민수 소프트웨어 엔지니어 경력 서울") == "ko"
        assert detect_content_language("王小明 软件工程师 工作经历 北京") == "zh"


class TestContentChecks:
    def test_cjk_name_causes_no_special_character_failure(self) -> None:
        text = "王小明\nwang@example.com +86 555 0100 2233\n工作经历\n教育背景\n技能\n2019 2021"
        checks = run_content_checks(text, content_language="zh")
        by_id = {check.id: check for check in checks}
        assert by_id["section_headings"].status == "pass"
        assert by_id["length"].status == "not_applicable"
        assert by_id["action_verbs"].status == "not_applicable"
        document = ExtractedDocument(file_format="docx", text=text)
        layout = {check.id: check for check in run_layout_checks(document)}
        assert layout["replacement_characters"].status == "pass"
        assert layout["icon_font_glyphs"].status == "pass"

    def test_unknown_language_never_applies_english_lexicon(self) -> None:
        checks = {c.id: c for c in run_content_checks("Python Go", content_language="unknown")}
        assert checks["action_verbs"].status == "not_applicable"
        assert checks["month_year_dates"].status == "not_applicable"

    def test_uppercase_heading_is_found(self) -> None:
        assert find_section_headings("WORK EXPERIENCE\nEDUCATION:\nTechnical Skills") == [
            "experience",
            "education",
            "skills",
        ]

    def test_heading_inside_a_sentence_is_not_a_heading(self) -> None:
        text = "I have extensive experience with education technology and many skills in sales"
        assert find_section_headings(text) == []

    def test_month_year_dates_fail_when_only_years(self) -> None:
        checks = {c.id: c for c in run_content_checks("2019 - 2021\n2015 - 2019", content_language="en")}
        assert checks["dates_present"].status == "pass"
        assert checks["month_year_dates"].status == "fail"

    def test_replacement_characters_ratio_fails(self) -> None:
        text = "Resume \ufffd\ufffd\ufffd text"
        document = ExtractedDocument(file_format="docx", text=text)
        layout = {check.id: check for check in run_layout_checks(document)}
        assert layout["replacement_characters"].status == "fail"
        assert layout["replacement_characters"].params["count"] == 3


class TestScoring:
    def _check(self, severity: str, status: str = "fail", category: str = "layout") -> CheckResult:
        return CheckResult(
            id="x", category=category, severity=severity, status=status  # type: ignore[arg-type]
        )

    def test_fatal_caps_score_at_ten(self) -> None:
        assert overall_score([self._check("fatal")]) == 0
        assert overall_score([self._check("low", "pass")]) == 100
        assert overall_score([self._check("high"), self._check("low")]) == 76

    def test_profiles_weight_layout_by_strictness(self) -> None:
        results = {
            result.id: result
            for result in score_profiles(
                [self._check("high")], ["contact", "experience", "education", "skills"]
            )
        }
        assert results["workday"].score == 82
        assert results["lever"].score == 93
        assert [profile.id for profile in PROFILES] == [
            "workday",
            "taleo",
            "successfactors",
            "icims",
            "greenhouse",
            "lever",
        ]

    def test_missing_required_sections_cost_points(self) -> None:
        results = {r.id: r for r in score_profiles([], ["experience"])}
        assert results["workday"].score == 85
        assert results["lever"].score == 100


def test_message_catalog_covers_the_same_ids_for_pass_and_fail() -> None:
    assert set(FAIL_MESSAGES) == set(PASS_MESSAGES)


@pytest.mark.parametrize(
    ("a", "b"), list(itertools.combinations(["en", "es", "fr", "pt", "de"], 2))
)
def test_stopword_detection_distinguishes_latin_languages(a: str, b: str) -> None:
    samples = {
        "en": "the team and the product in the company with the users of the platform for the year",
        "es": "el equipo y los productos de la empresa con los usuarios para el año y el mercado",
        "fr": "le travail et les produits de la société avec les clients pour le marché et les équipes",
        "pt": "o time e os produtos da empresa com os usuários para o mercado e os clientes",
        "de": "der Bereich und die Produkte mit den Kunden für das Team und die Firma bei der Arbeit",
    }
    assert detect_content_language(samples[a] * 3) == a
    assert detect_content_language(samples[b] * 3) == b
