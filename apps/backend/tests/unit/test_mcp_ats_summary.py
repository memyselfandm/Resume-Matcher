"""Unit tests for the MCP ATS tools' verdict and summary helpers."""

from typing import Any

import pytest

from app.mcp.bridge import BridgeError
from app.mcp.tools.ats import (
    DEFAULT_RETRY_AFTER_SECONDS,
    _retry_after_seconds,
    _template_settings,
    failing_checks,
    own_output_summary,
    present_verification,
    report_summary,
    verdict,
)
from app.services.ats_parse.own_output import (
    OwnOutputParseCheck,
    TemplateParseCheck,
    TemplateSettings,
)
from app.services.ats_parse.report import (
    CheckResult,
    ParseCheckReport,
    RoundtripResult,
)


def check(check_id: str, severity: str, status: str = "fail", **params: Any) -> CheckResult:
    category = "content" if check_id.startswith("contact") else "layout"
    return CheckResult(
        id=check_id, category=category, severity=severity, status=status, params=params
    )


def report(checks: list[CheckResult], recall: float | None = None) -> ParseCheckReport:
    roundtrip = (
        RoundtripResult(content_recall=recall, order_fidelity=0.9, fields=[])
        if recall is not None
        else None
    )
    return ParseCheckReport(
        file_format="pdf",
        extractability="full",
        content_language="en",
        overall_score=80,
        content_score=90,
        checks=checks,
        roundtrip=roundtrip,
        profiles=[],
        extracted_text_preview="",
    )


class TestVerdict:
    def test_recall_at_threshold_passes(self) -> None:
        assert verdict(report([], recall=0.95), 0.95) == (True, [])

    def test_recall_below_threshold_fails_with_reason(self) -> None:
        assert verdict(report([], recall=0.947), 0.95) == (
            False,
            ["content_recall 0.947 is below 0.95"],
        )

    def test_no_roundtrip_ignores_recall(self) -> None:
        assert verdict(report([]), 0.95) == (True, [])

    @pytest.mark.parametrize("severity", ["fatal", "high"])
    def test_blocking_severity_fails(self, severity: str) -> None:
        passes, reasons = verdict(report([check("tables", severity)], recall=1.0), 0.95)
        assert passes is False
        assert reasons == ["fatal/high checks failed: tables"]

    @pytest.mark.parametrize("severity", ["medium", "low"])
    def test_non_blocking_severity_passes(self, severity: str) -> None:
        assert verdict(report([check("tables", severity)], recall=1.0), 0.95)[0] is True

    def test_passed_high_check_does_not_block(self) -> None:
        assert verdict(report([check("tables", "high", status="pass")]), 0.95)[0] is True

    def test_expected_two_column_check_does_not_block(self) -> None:
        # The engine downgrades column checks of two-column templates to medium.
        column = check("multi_column", "medium", expected_by_template=True)
        assert verdict(report([column], recall=1.0), 0.95)[0] is True

    def test_both_reasons_are_reported(self) -> None:
        passes, reasons = verdict(report([check("text_layer", "fatal")], recall=0.1), 0.95)
        assert passes is False
        assert len(reasons) == 2


class TestSummaries:
    def test_failing_checks_render_english_and_flag_template_expectation(self) -> None:
        checks = [
            check("multi_column", "medium", expected_by_template=True),
            check("sidebar", "medium", status="pass"),
            check("page_count", "low", pages=3, max_pages=2),
        ]
        assert failing_checks(report(checks)) == [
            {
                "id": "multi_column",
                "severity": "medium",
                "message": (
                    "Multi-column layout detected. An ATS may read the columns out of "
                    "order. Expected for the selected two-column template."
                ),
                "expected_by_template": True,
            },
            {
                "id": "page_count",
                "severity": "low",
                "message": "The resume is 3 pages; most systems prefer 2 or fewer.",
            },
        ]

    def test_report_summary_fields(self) -> None:
        summary = report_summary(report([check("tables", "high")], recall=0.99), 0.95)
        assert summary["passes"] is False
        assert summary["parseability_score"] == 80
        assert summary["content_score"] == 90
        assert summary["content_recall"] == 0.99
        assert summary["order_fidelity"] == 0.9
        assert [item["id"] for item in summary["failing_checks"]] == ["tables"]
        assert summary["reasons"] == ["fatal/high checks failed: tables"]

    def test_report_summary_without_roundtrip_omits_recall(self) -> None:
        summary = report_summary(report([]), 0.95)
        assert "content_recall" not in summary and "order_fidelity" not in summary
        assert "reasons" not in summary

    def test_unrendered_template_reports_its_error(self) -> None:
        check_result = OwnOutputParseCheck(
            resume_id="r1",
            render_locale="en",
            settings=TemplateSettings(),
            results=[
                TemplateParseCheck(
                    template="vivid",
                    status="render_failed",
                    expected_by_template=True,
                    render_attempts=3,
                    error="render_busy",
                ),
                TemplateParseCheck(
                    template="clean",
                    status="ok",
                    expected_by_template=False,
                    render_attempts=1,
                    report=report([], recall=1.0),
                ),
            ],
        )
        vivid, clean = own_output_summary(check_result)["results"]
        assert vivid == {
            "template": "vivid",
            "status": "render_failed",
            "two_column_by_design": True,
            "error": "render_busy",
        }
        assert clean["passes"] is True and "two_column_by_design" not in clean
        assert "status" not in clean and "failing_checks" not in clean


    def test_unknown_check_falls_back_to_id_and_params(self) -> None:
        checks = [check("future_check", "high", foo=1), check("page_count", "low", pages=3)]
        assert failing_checks(report(checks)) == [
            {"id": "future_check", "severity": "high", "params": {"foo": 1}},
            # page_count's message needs max_pages, which is missing here.
            {"id": "page_count", "severity": "low", "params": {"pages": 3}},
        ]

    def test_shared_messages_are_listed_once(self) -> None:
        def template(name: str, pages: int) -> TemplateParseCheck:
            checks = [
                check("multi_column", "medium", expected_by_template=True),
                check("page_count", "low", pages=pages, max_pages=2),
            ]
            return TemplateParseCheck(
                template=name,
                status="ok",
                expected_by_template=True,
                render_attempts=1,
                report=report(checks, recall=1.0),
            )

        summary = own_output_summary(
            OwnOutputParseCheck(
                resume_id="r1",
                render_locale="en",
                settings=TemplateSettings(),
                results=[template("swiss-two-column", 3), template("vivid", 4)],
            )
        )
        assert list(summary["messages"]) == ["multi_column"]
        assert summary["messages"]["multi_column"].endswith(
            "Expected for the selected two-column template."
        )
        first, second = summary["results"]
        assert first["failing_checks"][0] == {
            "id": "multi_column",
            "severity": "medium",
            "expected_by_template": True,
        }
        # Different wording per template stays inline.
        assert first["failing_checks"][1]["message"].startswith("The resume is 3 pages")
        assert second["failing_checks"][1]["message"].startswith("The resume is 4 pages")


class TestPresentVerification:
    def stored(self, recall: float) -> dict[str, Any]:
        check_result = OwnOutputParseCheck(
            resume_id="t1",
            render_locale="en",
            settings=TemplateSettings(),
            results=[
                TemplateParseCheck(
                    template="swiss-single",
                    status="ok",
                    expected_by_template=False,
                    render_attempts=1,
                    report=report([], recall=recall),
                )
            ],
        )
        return {
            "tailored_resume_id": "t1",
            "application_id": "a1",
            "source_resume_id": "s1",
            "job_id": "j1",
            "template": "swiss-single",
            "keyword_score": 70.0,
            "keyword_score_detail": {"overall_score": 70.0},
            "warnings": [],
            "report": check_result.model_dump(mode="json"),
        }

    def test_verdict_follows_the_callers_threshold(self) -> None:
        stored = self.stored(recall=0.97)
        lenient = present_verification(stored, detail=False, min_content_recall=0.95)
        strict = present_verification(stored, detail=False, min_content_recall=0.99)
        assert (lenient["passes"], strict["passes"]) == (True, False)
        assert strict["min_content_recall"] == 0.99
        assert strict["reasons"] == ["content_recall 0.97 is below 0.99"]
        assert "report" not in lenient and "keyword_score_detail" not in lenient
        assert "warnings" not in lenient

    def test_detail_adds_the_stored_reports(self) -> None:
        stored = self.stored(recall=1.0)
        shown = present_verification(stored, detail=True, min_content_recall=0.95)
        assert shown["report"] == stored["report"]
        assert shown["keyword_score_detail"] == {"overall_score": 70.0}
        assert shown["tailored_resume_id"] == "t1" and shown["application_id"] == "a1"


class TestArguments:
    def test_shortcuts_override_settings_without_mutating_them(self) -> None:
        settings = TemplateSettings(template="latex", pageSize="LETTER")
        merged = _template_settings(settings, template="clean", render_locale="ja")
        assert (merged.template, merged.pageSize, merged.lang) == ("clean", "LETTER", "ja")
        assert (settings.template, settings.lang) == ("latex", None)

    def test_defaults_without_settings(self) -> None:
        assert _template_settings(None) == TemplateSettings()

    @pytest.mark.parametrize(
        ("header", "seconds"),
        [("7", 7.0), (None, DEFAULT_RETRY_AFTER_SECONDS), ("Wed, 21 Oct 2026", DEFAULT_RETRY_AFTER_SECONDS)],
    )
    def test_retry_after_parsing(self, header: str | None, seconds: float) -> None:
        assert _retry_after_seconds(BridgeError("busy", 429, header)) == seconds
