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
        vivid, clean = own_output_summary(check_result)
        assert vivid == {
            "template": "vivid",
            "status": "render_failed",
            "two_column_by_design": True,
            "error": "render_busy",
        }
        assert clean["passes"] is True and "two_column_by_design" not in clean


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
