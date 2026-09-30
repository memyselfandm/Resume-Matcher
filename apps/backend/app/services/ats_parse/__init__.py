"""Deterministic ATS parse-check engine.

Answers "can an ATS-style text extractor recover this resume's content from
the file?" with layout, extraction, and content checks. Complementary to the
keyword score in ``app/services/ats.py``; no LLM is involved.
"""

from app.services.ats_parse.engine import (
    PARSE_CHECK_TIMEOUT_SECONDS,
    build_report,
    check_document_sync,
    run_parse_check,
)
from app.services.ats_parse.report import ParseCheckReport, report_to_json

__all__ = [
    "PARSE_CHECK_TIMEOUT_SECONDS",
    "ParseCheckReport",
    "build_report",
    "check_document_sync",
    "report_to_json",
    "run_parse_check",
]
