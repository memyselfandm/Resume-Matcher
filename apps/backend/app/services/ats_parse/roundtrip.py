"""Round-trip self-consistency: how much of the source resume survives extraction.

Compares the payload a template renders (``ResumeData``-shaped dict, including
``sectionMeta``) with the text an ATS-style extractor recovered from the
rendered file. This is self-consistency, not a vendor-ATS simulation.

* ``content_recall``: share of expected fields found (order-free).
* ``order_fidelity``: Kendall-tau agreement, rescaled to [0, 1], between the
  expected render order of found fields and their order in the extracted text.

Fields in hidden sections are ``hidden`` and fields a template does not print
(per its rendered-field map) are ``not_rendered``; neither counts as missing.
Both sides are normalized with ``normalize_text`` so CSS-uppercased headings,
ligatures, typographic dashes, and HTML bullets still match.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from app.services.ats_parse.normalize import normalize_text, tokenize
from app.services.ats_parse.report import FieldStatus, RoundtripField, RoundtripResult

FOUND_MIN_RATIO = 0.9
GARBLED_MIN_RATIO = 0.5
SHORT_FIELD_MAX_TOKENS = 2
WINDOW_SLACK_TOKENS = 2

DEFAULT_SECTION_ORDER = (
    "summary",
    "workExperience",
    "education",
    "personalProjects",
    "additional",
)
_PERSONAL_FIELDS = ("name", "title", "email", "phone", "location", "website", "linkedin", "github")
_ADDITIONAL_FIELDS = ("technicalSkills", "languages", "certificationsTraining", "awards")


@dataclass(frozen=True)
class ExpectedField:
    """One source value the rendered file should contain."""

    path: str
    kind: str
    value: str
    hidden: bool


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _entry_fields(
    section: str, index: int, entry: dict[str, Any], names: tuple[str, ...], hidden: bool
) -> list[ExpectedField]:
    fields: list[ExpectedField] = []
    for name in names:
        value = entry.get(name)
        if isinstance(value, list):
            for position, item in enumerate(value):
                fields.append(
                    ExpectedField(
                        f"{section}[{index}].{name}[{position}]",
                        f"{section}.{name}",
                        _text(item),
                        hidden,
                    )
                )
        else:
            fields.append(
                ExpectedField(f"{section}[{index}].{name}", f"{section}.{name}", _text(value), hidden)
            )
    return fields


def _section_fields(
    key: str, source: dict[str, Any], hidden: bool
) -> list[ExpectedField]:
    if key == "summary":
        return [ExpectedField("summary", "summary", _text(source.get("summary")), hidden)]
    entry_names: dict[str, tuple[str, ...]] = {
        "workExperience": ("title", "company", "location", "years", "description"),
        "education": ("degree", "institution", "years", "description"),
        "personalProjects": ("name", "role", "years", "description"),
    }
    if key in entry_names:
        entries = source.get(key) or []
        return [
            field
            for index, entry in enumerate(entries)
            if isinstance(entry, dict)
            for field in _entry_fields(key, index, entry, entry_names[key], hidden)
        ]
    if key == "additional":
        additional = source.get("additional") or {}
        return [
            ExpectedField(f"additional.{name}[{index}]", f"additional.{name}", _text(item), hidden)
            for name in _ADDITIONAL_FIELDS
            for index, item in enumerate(additional.get(name) or [])
        ]
    custom = (source.get("customSections") or {}).get(key)
    if not isinstance(custom, dict):
        return []
    fields: list[ExpectedField] = []
    for index, item in enumerate(custom.get("items") or []):
        if isinstance(item, dict):
            fields.extend(
                _entry_fields(
                    f"customSections.{key}",
                    index,
                    item,
                    ("title", "subtitle", "location", "years", "description"),
                    hidden,
                )
            )
    for index, item in enumerate(custom.get("strings") or []):
        fields.append(
            ExpectedField(
                f"customSections.{key}.strings[{index}]", "customSections.strings", _text(item), hidden
            )
        )
    if custom.get("text"):
        fields.append(
            ExpectedField(
                f"customSections.{key}.text", "customSections.text", _text(custom["text"]), hidden
            )
        )
    return fields


def expected_fields(source: dict[str, Any]) -> list[ExpectedField]:
    """List every non-empty source value in render order (personal info first)."""
    personal = source.get("personalInfo") or {}
    fields = [
        ExpectedField(f"personalInfo.{name}", f"personalInfo.{name}", _text(personal.get(name)), False)
        for name in _PERSONAL_FIELDS
    ]
    meta = [entry for entry in source.get("sectionMeta") or [] if isinstance(entry, dict)]
    if meta:
        ordered = sorted(meta, key=lambda entry: (entry.get("order", 0), str(entry.get("id", ""))))
        sections = [
            (str(entry.get("key", "")), _text(entry.get("displayName")), not entry.get("isVisible", True))
            for entry in ordered
            if entry.get("key") != "personalInfo"
        ]
    else:
        sections = [(key, "", False) for key in DEFAULT_SECTION_ORDER]
    for key, heading, hidden in sections:
        section_fields = _section_fields(key, source, hidden)
        if heading and any(field.value.strip() for field in section_fields):
            fields.append(ExpectedField(f"heading.{key}", "heading", heading, hidden))
        fields.extend(section_fields)
    return [field for field in fields if normalize_text(field.value, source_html=True)]


def _best_window(needle: list[str], haystack: list[str]) -> tuple[float, int]:
    """Best multiset coverage of ``needle`` by a haystack window, and its start."""
    need = Counter(needle)
    size = min(len(haystack), len(needle) + WINDOW_SLACK_TOKENS)
    if size == 0:
        return 0.0, -1
    window: Counter[str] = Counter()
    matched = 0
    best, best_start = 0, -1
    for index, token in enumerate(haystack):
        if window[token] < need[token]:
            matched += 1
        window[token] += 1
        if index >= size:
            leaving = haystack[index - size]
            window[leaving] -= 1
            if window[leaving] < need[leaving]:
                matched -= 1
        if matched > best:
            best, best_start = matched, max(0, index - size + 1)
    return best / len(needle), best_start


def _locate(needle: list[str], haystack: list[str], joined: str) -> tuple[float, int]:
    """Exact contiguous match first; otherwise the best fuzzy window."""
    target = " " + " ".join(needle) + " "
    offset = joined.find(target)
    if offset >= 0:
        return 1.0, joined.count(" ", 0, offset)
    return _best_window(needle, haystack)


def _kendall_fidelity(positions: list[int]) -> float:
    concordant = discordant = 0
    for i, first in enumerate(positions):
        for second in positions[i + 1 :]:
            if first < second:
                concordant += 1
            elif first > second:
                discordant += 1
    if concordant + discordant == 0:
        return 1.0
    tau = (concordant - discordant) / (concordant + discordant)
    return (tau + 1) / 2


def compute_roundtrip(
    source: dict[str, Any],
    extracted_text: str,
    *,
    rendered_fields: frozenset[str] | None = None,
) -> RoundtripResult:
    """Score how completely and in what order source fields survive extraction.

    Args:
        source: The payload the template rendered (``processed_resume``).
        extracted_text: Text recovered from the rendered file.
        rendered_fields: Field kinds (e.g. ``"workExperience.location"``,
            ``"heading"``) the template prints; others become ``not_rendered``.
            ``None`` means every field is expected.
    """
    haystack = tokenize(normalize_text(extracted_text))
    joined = " " + " ".join(haystack) + " "
    results: list[RoundtripField] = []
    positions: list[int] = []
    found = considered = 0
    for field in expected_fields(source):
        status: FieldStatus
        if field.hidden:
            results.append(RoundtripField(field=field.path, status="hidden", score=0.0))
            continue
        if rendered_fields is not None and field.kind not in rendered_fields:
            results.append(RoundtripField(field=field.path, status="not_rendered", score=0.0))
            continue
        needle = tokenize(normalize_text(field.value, source_html=True))
        if not needle:
            continue
        ratio, position = _locate(needle, haystack, joined)
        found_threshold = 1.0 if len(needle) <= SHORT_FIELD_MAX_TOKENS else FOUND_MIN_RATIO
        if ratio >= found_threshold:
            status = "found"
            found += 1
            positions.append(position)
        elif ratio >= GARBLED_MIN_RATIO:
            status = "garbled"
        else:
            status = "missing"
        considered += 1
        results.append(RoundtripField(field=field.path, status=status, score=round(ratio, 3)))
    recall = found / considered if considered else 1.0
    return RoundtripResult(
        content_recall=round(recall, 3),
        order_fidelity=round(_kendall_fidelity(positions), 3),
        fields=results,
    )
