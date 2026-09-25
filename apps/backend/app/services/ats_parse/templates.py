"""What each resume template prints, and in which order.

Mirrors the seven templates in ``apps/frontend/components/resume/`` so the
round-trip compares extracted text with what a template actually rendered:

* ``rendered_fields``: field kinds the template prints. A source field whose
  kind is absent is reported ``not_rendered``, never ``missing``.
* ``personal_order``: order of the contact header fields.
* ``body_order``: ``None`` when sections follow ``sectionMeta`` order; else
  the template's fixed layout (DOM) order of section groups.
* ``two_column``: the layout is two-column by design, so ``multi_column`` and
  ``sidebar`` failures are expected for it.

Every template prints every personal, entry, and additional-list field; the
two-column templates split the "additional" section into fixed, localized
headings (skills, languages, certifications, awards) and never print its
``sectionMeta`` display name. Keep this module in sync with the templates.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Literal

TemplateId = Literal[
    "swiss-single",
    "swiss-two-column",
    "modern",
    "modern-two-column",
    "latex",
    "clean",
    "vivid",
]
TEMPLATE_IDS: tuple[TemplateId, ...] = (
    "swiss-single",
    "swiss-two-column",
    "modern",
    "modern-two-column",
    "latex",
    "clean",
    "vivid",
)

# ``locales`` of ``apps/frontend/i18n/config.ts``: the print page resolves any
# other ``lang`` to English. Portuguese is ``pt`` (its strings live in
# ``messages/pt-BR.json``, mapped by ``lib/i18n/messages.ts``).
RenderLocale = Literal["en", "es", "zh", "ja", "pt", "fr", "ko"]
DEFAULT_RENDER_LOCALE = "en"

PERSONAL_KINDS = (
    "personalInfo.name",
    "personalInfo.title",
    "personalInfo.email",
    "personalInfo.phone",
    "personalInfo.location",
    "personalInfo.website",
    "personalInfo.linkedin",
    "personalInfo.github",
)
ALL_FIELD_KINDS = frozenset(
    {
        *PERSONAL_KINDS,
        "heading",
        "summary",
        *(
            f"workExperience.{name}"
            for name in ("title", "company", "location", "years", "description")
        ),
        *(f"education.{name}" for name in ("degree", "institution", "years", "description")),
        *(f"personalProjects.{name}" for name in ("name", "role", "years", "description")),
        "additional.heading",
        "additional.technicalSkills",
        "additional.languages",
        "additional.certificationsTraining",
        "additional.awards",
        *(
            f"customSections.{name}"
            for name in ("title", "subtitle", "location", "years", "description", "strings", "text")
        ),
    }
)


@dataclass(frozen=True)
class TemplateLayout:
    """Rendered-field map and render order of one template."""

    rendered_fields: frozenset[str]
    personal_order: tuple[str, ...]
    body_order: tuple[str, ...] | None
    two_column: bool


_HEADER_EMAIL_FIRST = PERSONAL_KINDS
_HEADER_LOCATION_FIRST = (
    "personalInfo.name",
    "personalInfo.title",
    "personalInfo.location",
    "personalInfo.phone",
    "personalInfo.email",
    "personalInfo.linkedin",
    "personalInfo.github",
    "personalInfo.website",
)
_HEADER_LINKS_FIRST = (
    "personalInfo.name",
    "personalInfo.title",
    "personalInfo.website",
    "personalInfo.linkedin",
    "personalInfo.github",
    "personalInfo.email",
    "personalInfo.phone",
    "personalInfo.location",
)
_TWO_COLUMN_FIELDS = ALL_FIELD_KINDS - {"additional.heading"}
# Main column, then sidebar (``resume-two-column.tsx``, ``resume-modern-two-column.tsx``).
_SWISS_TWO_COLUMN_BODY = (
    "summary",
    "workExperience",
    "personalProjects",
    "additional.certificationsTraining",
    "custom",
    "education",
    "additional.technicalSkills",
    "additional.languages",
    "additional.awards",
)
# ``resume-vivid.tsx``: its sidebar lists skills and languages before education.
_VIVID_BODY = (
    "summary",
    "workExperience",
    "personalProjects",
    "additional.certificationsTraining",
    "custom",
    "additional.technicalSkills",
    "additional.languages",
    "education",
    "additional.awards",
)

TEMPLATE_LAYOUTS: dict[str, TemplateLayout] = {
    "swiss-single": TemplateLayout(ALL_FIELD_KINDS, _HEADER_EMAIL_FIRST, None, False),
    "swiss-two-column": TemplateLayout(
        _TWO_COLUMN_FIELDS, _HEADER_EMAIL_FIRST, _SWISS_TWO_COLUMN_BODY, True
    ),
    "modern": TemplateLayout(ALL_FIELD_KINDS, _HEADER_EMAIL_FIRST, None, False),
    "modern-two-column": TemplateLayout(
        _TWO_COLUMN_FIELDS, _HEADER_EMAIL_FIRST, _SWISS_TWO_COLUMN_BODY, True
    ),
    "latex": TemplateLayout(ALL_FIELD_KINDS, _HEADER_LOCATION_FIRST, None, False),
    "clean": TemplateLayout(ALL_FIELD_KINDS, _HEADER_LOCATION_FIRST, None, False),
    "vivid": TemplateLayout(_TWO_COLUMN_FIELDS, _HEADER_LINKS_FIRST, _VIVID_BODY, True),
}

# ``resume.sections.*`` of each locale's message file (``lib/i18n/messages.ts``
# maps locale -> file): the print page swaps a default section's English
# display name for these. ``tests/unit/test_ats_parse_own_renders.py`` asserts
# parity with the frontend locale list and message files.
_DEFAULT_ENGLISH_NAMES = {
    "summary": "Summary",
    "workExperience": "Experience",
    "education": "Education",
    "personalProjects": "Projects",
    "additional": "Skills & Awards",
}
LOCALIZED_DEFAULT_HEADINGS: dict[str, dict[str, str]] = {
    "en": _DEFAULT_ENGLISH_NAMES,
    "es": {
        "summary": "Resumen",
        "workExperience": "Experiencia",
        "education": "Educación",
        "personalProjects": "Proyectos",
        "additional": "Habilidades y Premios",
    },
    "fr": {
        "summary": "Résumé",
        "workExperience": "Expérience",
        "education": "Formation",
        "personalProjects": "Projets",
        "additional": "Compétences et récompenses",
    },
    "ja": {
        "summary": "概要",
        "workExperience": "職歴",
        "education": "学歴",
        "personalProjects": "プロジェクト",
        "additional": "スキル・受賞歴",
    },
    "ko": {
        "summary": "요약",
        "workExperience": "경력",
        "education": "학력",
        "personalProjects": "프로젝트",
        "additional": "기술 및 수상 내역",
    },
    "pt": {
        "summary": "Resumo",
        "workExperience": "Experiência",
        "education": "Formação",
        "personalProjects": "Projetos",
        "additional": "Habilidades e Prêmios",
    },
    "zh": {
        "summary": "个人简介",
        "workExperience": "工作经历",
        "education": "教育背景",
        "personalProjects": "项目经历",
        "additional": "技能与荣誉",
    },
}


def localize_section_meta(source: dict[str, Any], render_locale: str | None) -> dict[str, Any]:
    """Return ``source`` with default section names localized like the print page.

    Mirrors ``withLocalizedDefaultSections``: only built-in sections whose
    display name is still the English default are renamed. The input is not
    modified.
    """
    headings = LOCALIZED_DEFAULT_HEADINGS.get(render_locale or DEFAULT_RENDER_LOCALE)
    meta = source.get("sectionMeta")
    if not headings or not isinstance(meta, list):
        return source
    localized = copy.deepcopy(source)
    for entry in localized["sectionMeta"]:
        if not isinstance(entry, dict) or not entry.get("isDefault"):
            continue
        section_id = str(entry.get("id", ""))
        english = _DEFAULT_ENGLISH_NAMES.get(section_id)
        if english is not None and entry.get("displayName") == english:
            entry["displayName"] = headings[section_id]
    return localized
