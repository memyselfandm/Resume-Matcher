"""Content checks on extracted resume text.

Ported in part from hugounoclaw/ats-checker (MIT License, Copyright (c) 2026
hugounoclaw): contact, standard-section, action-verb, quantification, length,
and date signals of ``assets/score.js`` (``analyze``, lines 36-90); and from
sunnypatell/ats-screener (MIT License, Copyright (c) 2026 Sunny Patel): the
action-verb list and quantification patterns of
``src/lib/engine/scorer/experience-scorer.ts`` and the section-header synonyms
of ``src/lib/engine/parser/section-detector.ts``. The keyword/job-description
half of ats-checker is intentionally not ported; keyword scoring already lives
in ``app/services/ats.py``.

Two language inputs are kept separate:
    ``content_language`` is the language the resume text is written in. It
    gates English-lexicon checks (action verbs, month-name dates), which are
    ``not_applicable`` for any other or an ``unknown`` language, so a resume is
    never penalized on a guess.
    ``render_locale`` is the locale that localized rendered section headings.
    When given, only that locale's headings are expected; otherwise headings in
    any supported language are accepted.
"""

from __future__ import annotations

import re

from app.services.ats_parse.normalize import normalize_text, tokenize
from app.services.ats_parse.report import CheckResult

SUPPORTED_CONTENT_LANGUAGES = ("en", "es", "fr", "pt", "de", "ja", "ko", "zh")
UNKNOWN_LANGUAGE = "unknown"

# Every pattern below runs on untrusted extracted text of up to 200k chars,
# so each is linear: quantifiers are bounded and matches are anchored with
# lookbehinds instead of relying on backtracking from every position.
EMAIL_RE = re.compile(
    r"(?<![a-z0-9._%+\-])[a-z0-9._%+\-]{1,64}@[a-z0-9\-]{1,63}"
    r"(?:\.[a-z0-9\-]{1,63}){0,8}\.[a-z]{2,24}",
    re.IGNORECASE,
)
_PHONE_CANDIDATE_RE = re.compile(r"(?<![\d+])\+?\d[\d \t().\-]{6,80}\d(?!\d)")
_DIGIT_GROUP_RE = re.compile(r"\d{1,15}")
PHONE_MIN_DIGITS = 10
PHONE_MAX_DIGITS = 15
LINKEDIN_RE = re.compile(r"linkedin\.com/(?:in|pub)/[\w\-%.]{1,100}", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_MONTH_YEAR_RE = re.compile(
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?"
    r"|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    r"\.?\s{1,3}(?:19|20)\d{2}\b"
    r"|\b(?:0?[1-9]|1[0-2])/(?:19|20)\d{2}\b",
    re.IGNORECASE,
)

# Union of ats-checker ACTION and ats-screener STRONG_ACTION_VERBS.
ACTION_VERBS = frozenset(
    """
    accelerated achieved administered advanced analyzed architected audited
    automated boosted budgeted built centralized championed collaborated
    conceptualized consolidated contributed converted coordinated created
    decreased delivered deployed designed developed directed drove eliminated
    enabled engineered established exceeded executed expanded facilitated
    forecasted founded generated grew headed identified implemented improved
    increased influenced initiated innovated integrated introduced launched led
    leveraged managed maximized mentored migrated modernized negotiated operated
    optimized orchestrated organized outperformed overhauled oversaw owned
    pioneered planned presented prioritized produced programmed proposed
    published raised recommended redesigned reduced refactored reformed
    re-engineered reorganized replaced researched resolved restructured revamped
    revolutionized scaled secured shipped simplified spearheaded standardized
    streamlined strengthened supervised surpassed synchronized trained
    transformed translated unified upgraded
    """.split()
)
ACTION_VERB_MIN_DISTINCT = 8

# Language-neutral quantification patterns (ats-screener experience-scorer.ts).
_QUANTIFICATION_PATTERNS = (
    re.compile(r"(?<!\d)\d{1,12}\s?%"),
    re.compile(r"[$€£¥]\s?\d"),
    re.compile(r"(?<![\d.])\d{1,12}(?:\.\d{1,6})?\s?[x×]\b", re.IGNORECASE),
    re.compile(r"(?<![\d,.])\d{1,3}(?:[,.]\d{3}){1,6}(?![\d,.]\d)"),
    re.compile(r"(?<!\d)\d{1,12}\+"),
)
QUANTIFIED_MIN_LINES = 3

MIN_WORDS = 150
MAX_WORDS = 1500
MIN_YEAR_MENTIONS = 2

# Canonical section -> heading phrases per language. Locale strings mirror the
# frontend print headings (apps/frontend/messages/*.json resume.sections);
# English synonyms come from ats-screener section-detector.ts.
SECTION_HEADINGS: dict[str, dict[str, tuple[str, ...]]] = {
    "en": {
        "experience": (
            "experience",
            "work experience",
            "professional experience",
            "employment",
            "employment history",
            "work history",
            "relevant experience",
            "career history",
        ),
        "education": (
            "education",
            "academic background",
            "educational background",
            "qualifications",
            "academic qualifications",
        ),
        "skills": (
            "skills",
            "technical skills",
            "core competencies",
            "competencies",
            "areas of expertise",
            "proficiencies",
            "technologies",
            "skills & awards",
        ),
    },
    "es": {
        "experience": ("experiencia", "experiencia laboral", "experiencia profesional"),
        "education": ("educación", "formación", "formación académica"),
        "skills": ("habilidades", "habilidades técnicas", "competencias", "habilidades y premios"),
    },
    "fr": {
        "experience": ("expérience", "expérience professionnelle", "expériences"),
        "education": ("formation", "éducation", "diplômes"),
        "skills": ("compétences", "compétences techniques"),
    },
    "pt": {
        "experience": ("experiência", "experiência profissional"),
        "education": ("formação", "educação", "formação acadêmica"),
        "skills": ("habilidades", "competências"),
    },
    "de": {
        "experience": ("berufserfahrung", "erfahrung", "beruflicher werdegang"),
        "education": ("ausbildung", "bildung", "studium"),
        "skills": ("kenntnisse", "fähigkeiten", "kompetenzen"),
    },
    "ja": {"experience": ("職歴", "経歴"), "education": ("学歴",), "skills": ("スキル",)},
    "ko": {"experience": ("경력",), "education": ("학력",), "skills": ("기술",)},
    "zh": {"experience": ("工作经历",), "education": ("教育背景",), "skills": ("技能",)},
}
EXPECTED_SECTIONS = ("experience", "education", "skills")
HEADING_MAX_TOKENS = 5

_STOPWORDS: dict[str, frozenset[str]] = {
    "en": frozenset(
        "the and of to in for with on at by from as is are was were an a our "
        "this that my i".split()
    ),
    "es": frozenset(
        "el la los las de del y en con para por un una que se al como su mi "
        "es son lo".split()
    ),
    "fr": frozenset(
        "le la les de des du et en avec pour par un une que qui au aux sur "
        "est dans mon".split()
    ),
    "pt": frozenset(
        "o a os as de do da dos das e em com para por um uma que no na ao "
        "meu é".split()
    ),
    "de": frozenset(
        "der die das und in mit für von zu den dem des ein eine ist im auf "
        "bei als".split()
    ),
}
# Words shared by several languages ("de", "en", "la") say nothing about which
# one a text is written in, so only language-exclusive stopwords are counted.
_EXCLUSIVE_STOPWORDS: dict[str, frozenset[str]] = {
    language: words.difference(
        *(other for name, other in _STOPWORDS.items() if name != language)
    )
    for language, words in _STOPWORDS.items()
}
DETECTION_MIN_TOKENS = 30
DETECTION_MIN_RATIO = 0.03
DETECTION_MIN_MARGIN = 1.5
CJK_MIN_RATIO = 0.3


def _is_date_like(groups: list[str]) -> bool:
    """Digit groups made only of years and months, with at least two years.

    Covers "2019 - 2023", "04.2019 - 03.2021", "2019.04 - 2021.03", and
    "2019-04 - 2021-03" (common de/ja/ko/zh date formats).
    """
    years = sum(1 for group in groups if len(group) == 4 and group[:2] in ("19", "20"))
    months = sum(1 for group in groups if len(group) <= 2 and 1 <= int(group) <= 12)
    return years >= 2 and years + months == len(groups)


def has_phone(text: str) -> bool:
    """Whether text contains a phone number (10-15 digits, not a date range).

    A candidate is a bounded run of digits and phone separators. Every
    contiguous run of its digit groups is tested, so a phone followed by a
    date range ("555-555-0100 2019 - 2023") is still found, while runs made
    only of years and months are rejected.
    """
    for match in _PHONE_CANDIDATE_RE.finditer(text):
        groups = _DIGIT_GROUP_RE.findall(match.group(0))
        for first in range(len(groups)):
            digits = 0
            for last in range(first, len(groups)):
                digits += len(groups[last])
                if digits > PHONE_MAX_DIGITS:
                    break
                if digits >= PHONE_MIN_DIGITS and not _is_date_like(groups[first : last + 1]):
                    return True
    return False


def has_email(text: str) -> bool:
    """Whether text contains an email address."""
    return EMAIL_RE.search(text) is not None


def detect_content_language(text: str) -> str:
    """Guess the resume's language from script and stopword ratios.

    Returns a code from ``SUPPORTED_CONTENT_LANGUAGES`` or ``"unknown"`` when
    the evidence is weak; callers must treat ``unknown`` as "do not apply
    language-specific lexicons".
    """
    letters = [char for char in text if char.isalpha()]
    if letters:
        kana = sum(1 for c in letters if "\u3040" <= c <= "\u30ff")
        hangul = sum(1 for c in letters if "\uac00" <= c <= "\ud7af" or "\u1100" <= c <= "\u11ff")
        han = sum(1 for c in letters if "\u4e00" <= c <= "\u9fff")
        cjk = kana + hangul + han
        if cjk / len(letters) >= CJK_MIN_RATIO:
            if kana:
                return "ja"
            if hangul >= han:
                return "ko"
            return "zh"
    tokens = tokenize(normalize_text(text))
    if len(tokens) < DETECTION_MIN_TOKENS:
        return UNKNOWN_LANGUAGE
    counts = {
        language: sum(1 for token in tokens if token in words)
        for language, words in _EXCLUSIVE_STOPWORDS.items()
    }
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    (best, best_count), (_, runner_up) = ranked[0], ranked[1]
    if best_count / len(tokens) < DETECTION_MIN_RATIO:
        return UNKNOWN_LANGUAGE
    if best_count < DETECTION_MIN_MARGIN * runner_up:
        return UNKNOWN_LANGUAGE
    return best


def _locale_language(render_locale: str) -> str:
    return render_locale.split("-")[0].lower()


def _heading_phrases(render_locale: str | None) -> dict[str, set[str]]:
    languages = (
        [_locale_language(render_locale)]
        if render_locale and _locale_language(render_locale) in SECTION_HEADINGS
        else sorted(SECTION_HEADINGS)
    )
    phrases: dict[str, set[str]] = {section: set() for section in EXPECTED_SECTIONS}
    for language in languages:
        for section, values in SECTION_HEADINGS[language].items():
            phrases[section].update(normalize_text(value) for value in values)
    return phrases


def find_section_headings(text: str, render_locale: str | None = None) -> list[str]:
    """Return the expected sections whose heading appears as its own short line."""
    phrases = _heading_phrases(render_locale)
    found: set[str] = set()
    for raw_line in text.splitlines():
        line = normalize_text(raw_line).strip(" :-|")
        if not line or len(tokenize(line)) > HEADING_MAX_TOKENS:
            continue
        for section, options in phrases.items():
            if any(line == option or line.startswith(option + " ") for option in options):
                found.add(section)
    return [section for section in EXPECTED_SECTIONS if section in found]


def _not_applicable(check_id: str, severity: str, reason: str) -> CheckResult:
    return CheckResult(
        id=check_id,
        category="content",
        severity=severity,  # type: ignore[arg-type]
        status="not_applicable",
        params={"reason": reason},
    )


_CONTENT_CHECKS: tuple[tuple[str, str], ...] = (
    ("contact_email", "high"),
    ("contact_phone", "medium"),
    ("contact_linkedin", "low"),
    ("section_headings", "medium"),
    ("dates_present", "medium"),
    ("month_year_dates", "low"),
    ("action_verbs", "low"),
    ("quantification", "low"),
    ("length", "medium"),
)


def run_content_checks(
    text: str,
    *,
    content_language: str,
    render_locale: str | None = None,
    has_text: bool = True,
) -> list[CheckResult]:
    """Run content checks in a fixed order; lexicon checks are language-gated."""
    if not has_text:
        return [
            _not_applicable(check_id, severity, "no_text")
            for check_id, severity in _CONTENT_CHECKS
        ]

    normalized = normalize_text(text)
    tokens = tokenize(normalized)
    checks: list[CheckResult] = []
    for check_id, severity, found_contact in (
        ("contact_email", "high", has_email(text)),
        ("contact_phone", "medium", has_phone(text)),
        ("contact_linkedin", "low", LINKEDIN_RE.search(text) is not None),
    ):
        checks.append(
            CheckResult(
                id=check_id,
                category="content",
                severity=severity,  # type: ignore[arg-type]
                status="pass" if found_contact else "fail",
            )
        )

    found = find_section_headings(text, render_locale)
    missing = [section for section in EXPECTED_SECTIONS if section not in found]
    checks.append(
        CheckResult(
            id="section_headings",
            category="content",
            severity="medium",
            status="fail" if missing else "pass",
            params={
                "found": found,
                "missing": missing,
                "render_locale": render_locale or "any",
            },
        )
    )

    years = len(_YEAR_RE.findall(text))
    checks.append(
        CheckResult(
            id="dates_present",
            category="content",
            severity="medium",
            status="pass" if years >= MIN_YEAR_MENTIONS else "fail",
            params={"count": years, "min_count": MIN_YEAR_MENTIONS},
        )
    )

    if content_language != "en":
        checks.append(_not_applicable("month_year_dates", "low", "content_language"))
        checks.append(_not_applicable("action_verbs", "low", "content_language"))
    else:
        month_dates = len(_MONTH_YEAR_RE.findall(text))
        checks.append(
            CheckResult(
                id="month_year_dates",
                category="content",
                severity="low",
                status="pass" if month_dates or not years else "fail",
                params={"count": month_dates},
            )
        )
        verbs = sorted(set(tokens) & ACTION_VERBS)
        checks.append(
            CheckResult(
                id="action_verbs",
                category="content",
                severity="low",
                status="pass" if len(verbs) >= ACTION_VERB_MIN_DISTINCT else "fail",
                params={"count": len(verbs), "min_count": ACTION_VERB_MIN_DISTINCT},
                evidence={"verbs": verbs[:10]},
            )
        )

    quantified = sum(
        1
        for line in text.splitlines()
        if any(pattern.search(line) for pattern in _QUANTIFICATION_PATTERNS)
    )
    checks.append(
        CheckResult(
            id="quantification",
            category="content",
            severity="low",
            status="pass" if quantified >= QUANTIFIED_MIN_LINES else "fail",
            params={"count": quantified, "min_count": QUANTIFIED_MIN_LINES},
        )
    )

    if content_language in ("ja", "ko", "zh"):
        checks.append(_not_applicable("length", "medium", "content_language"))
    else:
        words = len(text.split())
        verdict = "short" if words < MIN_WORDS else "long" if words > MAX_WORDS else "ok"
        checks.append(
            CheckResult(
                id="length",
                category="content",
                severity="medium",
                status="pass" if verdict == "ok" else "fail",
                params={
                    "words": words,
                    "verdict": verdict,
                    "min_words": MIN_WORDS,
                    "max_words": MAX_WORDS,
                },
            )
        )
    return checks


def detected_sections(text: str, render_locale: str | None = None) -> list[str]:
    """Canonical section names present, including contact (email or phone found)."""
    sections = list(find_section_headings(text, render_locale))
    if has_email(text) or has_phone(text):
        sections.insert(0, "contact")
    return sections
