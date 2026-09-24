"""Parse-check report contract.

The report carries machine-readable check ids and parameters only. Human text
is rendered by consumers: the frontend from its locale files and MCP/CLI
clients from ``messages_en``. Every float is rounded and every collection is
emitted in a fixed order so the same input serializes to byte-identical JSON.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = "2.0"

Severity = Literal["fatal", "high", "medium", "low"]
CheckStatus = Literal["pass", "fail", "not_applicable"]
Category = Literal["extraction", "layout", "content"]
Extractability = Literal["full", "partial", "none", "unsupported_format"]
FieldStatus = Literal["found", "garbled", "missing", "not_rendered", "hidden"]

SEVERITY_PENALTIES: dict[str, int] = {"fatal": 100, "high": 20, "medium": 10, "low": 4}
FATAL_SCORE_CAP = 10
EXTRACTED_TEXT_PREVIEW_CHARS = 1_000


class CheckResult(BaseModel):
    """Outcome of one deterministic check."""

    id: str
    category: Category
    severity: Severity
    status: CheckStatus
    params: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)


class RoundtripField(BaseModel):
    """Recovery status of one source field in the extracted text."""

    field: str
    status: FieldStatus
    score: float


class RoundtripResult(BaseModel):
    """Self-consistency of a rendered resume against its source payload."""

    content_recall: float
    order_fidelity: float
    fields: list[RoundtripField]


class ProfileResult(BaseModel):
    """Score against one heuristic ATS profile (not vendor-verified)."""

    id: str
    kind: Literal["heuristic"] = "heuristic"
    score: int
    passes: bool


class ParseCheckReport(BaseModel):
    """Full parse-check result for one document."""

    schema_version: str = SCHEMA_VERSION
    file_format: Literal["pdf", "docx", "doc"]
    extractability: Extractability
    content_language: str
    overall_score: int | None
    content_score: int | None
    checks: list[CheckResult]
    roundtrip: RoundtripResult | None = None
    profiles: list[ProfileResult]
    extracted_text_preview: str


PARSEABILITY_CATEGORIES = frozenset({"extraction", "layout"})


def _score(checks: list[CheckResult]) -> int:
    """Deduct a fixed penalty per failed check; any fatal failure caps the score."""
    failed = [check for check in checks if check.status == "fail"]
    score = 100 - sum(SEVERITY_PENALTIES[check.severity] for check in failed)
    score = max(0, min(100, score))
    if any(check.severity == "fatal" for check in failed):
        score = min(score, FATAL_SCORE_CAP)
    return score


def overall_score(checks: list[CheckResult]) -> int:
    """Parseability score from extraction and layout checks only.

    Content quality (contact details, headings, verbs) is reported separately
    as ``content_score`` so a well-parsed but thin resume is not reported as
    hard to parse.
    """
    return _score([check for check in checks if check.category in PARSEABILITY_CATEGORIES])


def content_score(checks: list[CheckResult]) -> int | None:
    """Content score from content checks; ``None`` when none of them applied."""
    content = [check for check in checks if check.category == "content"]
    if all(check.status == "not_applicable" for check in content):
        return None
    return _score(content)


def report_to_json(report: ParseCheckReport) -> str:
    """Serialize a report deterministically (sorted keys, compact separators)."""
    return json.dumps(
        report.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
