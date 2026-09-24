"""Heuristic ATS profiles.

Ported from sunnypatell/ats-screener (MIT License, Copyright (c) 2026 Sunny
Patel), ``src/lib/engine/scorer/profiles/*.ts``: parsing strictness, required
sections, and passing score for six widely used applicant tracking systems.

These are heuristic profiles, not vendor-verified behavior. Each profile
re-weights this engine's own check results: layout and extraction failures
are scaled by the profile's parsing strictness, and missing required sections
cost a fixed amount. Keyword matching is out of scope for parse checks.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.ats_parse.report import SEVERITY_PENALTIES, CheckResult, ProfileResult

CONTENT_PENALTY_SCALE = 0.5
MISSING_SECTION_PENALTY = 5


@dataclass(frozen=True)
class HeuristicProfile:
    """Parsing-strictness profile of one ATS family."""

    id: str
    name: str
    parsing_strictness: float
    required_sections: tuple[str, ...]
    passing_score: int


PROFILES: tuple[HeuristicProfile, ...] = (
    HeuristicProfile("workday", "Workday", 0.9, ("contact", "experience", "education", "skills"), 70),
    HeuristicProfile("taleo", "Taleo", 0.85, ("contact", "experience", "education", "skills"), 65),
    HeuristicProfile(
        "successfactors", "SuccessFactors", 0.85, ("contact", "experience", "education", "skills"), 65
    ),
    HeuristicProfile("icims", "iCIMS", 0.6, ("contact", "experience", "education"), 60),
    HeuristicProfile("greenhouse", "Greenhouse", 0.4, ("experience", "education"), 55),
    HeuristicProfile("lever", "Lever", 0.35, ("experience",), 50),
)


def score_profiles(
    checks: list[CheckResult], sections_present: list[str]
) -> list[ProfileResult]:
    """Score every profile from check outcomes and detected sections."""
    fatal = any(check.status == "fail" and check.severity == "fatal" for check in checks)
    results: list[ProfileResult] = []
    for profile in PROFILES:
        if fatal:
            results.append(ProfileResult(id=profile.id, score=0, passes=False))
            continue
        deduction = 0.0
        for check in checks:
            if check.status != "fail":
                continue
            scale = (
                CONTENT_PENALTY_SCALE
                if check.category == "content"
                else profile.parsing_strictness
            )
            deduction += SEVERITY_PENALTIES[check.severity] * scale
        missing = [
            section for section in profile.required_sections if section not in sections_present
        ]
        deduction += MISSING_SECTION_PENALTY * len(missing)
        score = max(0, min(100, round(100 - deduction)))
        results.append(
            ProfileResult(id=profile.id, score=score, passes=score >= profile.passing_score)
        )
    return results
