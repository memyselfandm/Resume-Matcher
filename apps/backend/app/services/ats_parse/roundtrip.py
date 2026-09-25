"""Round-trip self-consistency: how much of the source resume survives extraction.

Compares the payload a template renders (``ResumeData``-shaped dict, including
``sectionMeta``) with the text an ATS-style extractor recovered from the
rendered file. This is self-consistency, not a vendor-ATS simulation.

* ``content_recall``: share of expected fields found (order-free).
* ``order_fidelity``: Kendall-tau agreement, rescaled to [0, 1], between the
  expected render order of found fields and their order in the extracted text.

Fields in hidden sections are ``hidden`` and fields a template does not print
(per its rendered-field map) are ``not_rendered``; neither counts as missing.
Templates that place sections by a fixed layout instead of ``sectionMeta``
order (the two-column templates) pass their render order explicitly.
Both sides are normalized with ``normalize_text`` so CSS-uppercased headings,
ligatures, typographic dashes, and HTML bullets still match.

Entries (jobs, schools, projects, custom items) are located by walking the
text in render order: each entry is anchored at the first verbatim occurrence
of one of its identity fields (title, company, ...) after the previous
field's match, and every field found verbatim close to the cursor moves the
cursor past it. So a bullet that mentions the next entry's employer or title
("Shipped features for Google Maps") is consumed as part of its own entry
before the next entry is searched for. Every entry field, identity fields
included, must then be found inside its own entry's span.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import Counter
from dataclasses import dataclass
from typing import Any

from app.services.ats_parse.extract import check_deadline
from app.services.ats_parse.normalize import normalize_text, tokenize
from app.services.ats_parse.report import FieldStatus, RoundtripField, RoundtripResult

FOUND_MIN_RATIO = 0.9
GARBLED_MIN_RATIO = 0.5
SHORT_FIELD_MAX_TOKENS = 2
WINDOW_SLACK_TOKENS = 2
# Templates may print an entry's date or location just before its title.
ENTRY_BACK_SLACK_TOKENS = 8
# While walking the text in render order, a field only advances the cursor
# when it is found verbatim within this many tokens of the cursor, so one
# misplaced match can never skip over whole entries.
WALK_MAX_GAP_TOKENS = 12
# Expected fields compared per document; the rest are not scored and the
# result is marked ``truncated`` (a resume never comes close).
MAX_ROUNDTRIP_FIELDS = 1_000

DEFAULT_SECTION_ORDER = (
    "summary",
    "workExperience",
    "education",
    "personalProjects",
    "additional",
)
_PERSONAL_FIELDS = ("name", "title", "email", "phone", "location", "website", "linkedin", "github")
_ADDITIONAL_FIELDS = ("technicalSkills", "languages", "certificationsTraining", "awards")
_BUILTIN_SECTIONS = frozenset(DEFAULT_SECTION_ORDER)
CUSTOM_GROUP = "custom"


@dataclass(frozen=True)
class ExpectedField:
    """One source value the rendered file should contain.

    ``group`` names the layout unit the field is placed with: a personal field
    kind, a built-in section key, an additional-list kind, or ``custom``.
    """

    path: str
    kind: str
    value: str
    hidden: bool
    entry: str | None = None
    anchor: bool = False
    group: str = ""


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


# Fields that identify an entry; they anchor where the entry sits in the text.
_IDENTITY_FIELDS = frozenset({"title", "company", "degree", "institution", "name", "role", "subtitle"})


def _entry_fields(
    section: str,
    index: int,
    entry: dict[str, Any],
    names: tuple[str, ...],
    hidden: bool,
    group: str,
    kind_prefix: str | None = None,
) -> list[ExpectedField]:
    """Fields of one entry; kinds are ``{kind_prefix or section}.{name}``."""
    entry_id = f"{section}[{index}]"
    prefix = kind_prefix or section
    fields: list[ExpectedField] = []
    for name in names:
        value = entry.get(name)
        if isinstance(value, list):
            for position, item in enumerate(value):
                fields.append(
                    ExpectedField(
                        f"{entry_id}.{name}[{position}]",
                        f"{prefix}.{name}",
                        _text(item),
                        hidden,
                        entry=entry_id,
                        group=group,
                    )
                )
        else:
            fields.append(
                ExpectedField(
                    f"{entry_id}.{name}",
                    f"{prefix}.{name}",
                    _text(value),
                    hidden,
                    entry=entry_id,
                    anchor=name in _IDENTITY_FIELDS,
                    group=group,
                )
            )
    return fields


def _section_fields(
    key: str, source: dict[str, Any], hidden: bool
) -> list[ExpectedField]:
    if key == "summary":
        return [
            ExpectedField("summary", "summary", _text(source.get("summary")), hidden, group="summary")
        ]
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
            for field in _entry_fields(key, index, entry, entry_names[key], hidden, key)
        ]
    if key == "additional":
        additional = source.get("additional") or {}
        return [
            ExpectedField(
                f"additional.{name}[{index}]",
                f"additional.{name}",
                _text(item),
                hidden,
                group=f"additional.{name}",
            )
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
                    CUSTOM_GROUP,
                    kind_prefix="customSections",
                )
            )
    for index, item in enumerate(custom.get("strings") or []):
        fields.append(
            ExpectedField(
                f"customSections.{key}.strings[{index}]",
                "customSections.strings",
                _text(item),
                hidden,
                group=CUSTOM_GROUP,
            )
        )
    if custom.get("text"):
        fields.append(
            ExpectedField(
                f"customSections.{key}.text",
                "customSections.text",
                _text(custom["text"]),
                hidden,
                group=CUSTOM_GROUP,
            )
        )
    return fields


def _heading_field(key: str, heading: str, hidden: bool) -> ExpectedField:
    if key == "additional":
        # Two-column templates replace this heading with fixed per-list ones.
        return ExpectedField(
            "heading.additional", "additional.heading", heading, hidden, group="additional.heading"
        )
    group = key if key in _BUILTIN_SECTIONS else CUSTOM_GROUP
    return ExpectedField(f"heading.{key}", "heading", heading, hidden, group=group)


def expected_fields(source: dict[str, Any]) -> list[ExpectedField]:
    """List every non-empty source value in render order (personal info first)."""
    personal = source.get("personalInfo") or {}
    fields = [
        ExpectedField(
            f"personalInfo.{name}",
            f"personalInfo.{name}",
            _text(personal.get(name)),
            False,
            group=f"personalInfo.{name}",
        )
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
            fields.append(_heading_field(key, heading, hidden))
        fields.extend(section_fields)
    return [field for field in fields if normalize_text(field.value, source_html=True)]


def order_fields(
    fields: list[ExpectedField],
    personal_order: tuple[str, ...] | None = None,
    body_order: tuple[str, ...] | None = None,
) -> list[ExpectedField]:
    """Reorder fields to a template's render order (stable within a group).

    Personal fields come first in ``personal_order``; the body keeps
    ``sectionMeta`` order unless the template has a fixed ``body_order``.
    Groups missing from an order keep their relative order at its end.
    """
    personal = [field for field in fields if field.group.startswith("personalInfo.")]
    body = [field for field in fields if not field.group.startswith("personalInfo.")]
    if personal_order is not None:
        rank = {group: index for index, group in enumerate(personal_order)}
        personal.sort(key=lambda field: rank.get(field.group, len(rank)))
    if body_order is not None:
        rank = {group: index for index, group in enumerate(body_order)}
        body.sort(key=lambda field: rank.get(field.group, len(rank)))
    return personal + body


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


class _Haystack(list[str]):
    """Extracted tokens plus their joined text, so exact searches are one
    ``str.find`` over a slice of a string built once, not a re-join per call."""

    def __init__(self, tokens: list[str]) -> None:
        super().__init__(tokens)
        self.text = " " + " ".join(tokens) + " "
        # offsets[i] is the index of the space before token i; offsets[n] is
        # the trailing space.
        self.offsets: list[int] = []
        position = 0
        for token in tokens:
            self.offsets.append(position)
            position += len(token) + 1
        self.offsets.append(position)


def _find_exact(
    needle: list[str], haystack: _Haystack, start: int = 0, end: int | None = None
) -> int:
    """Token position of the first contiguous occurrence in [start, end), or -1."""
    count = len(haystack)
    start = max(0, min(start, count))
    end = count if end is None else max(start, min(end, count))
    offset = haystack.text.find(
        " " + " ".join(needle) + " ", haystack.offsets[start], haystack.offsets[end] + 1
    )
    if offset < 0:
        return -1
    return bisect_left(haystack.offsets, offset)


def _locate(
    needle: list[str], haystack: _Haystack, start: int = 0, end: int | None = None
) -> tuple[float, int]:
    """Exact contiguous match first, else the best fuzzy window, within [start, end)."""
    position = _find_exact(needle, haystack, start, end)
    if position >= 0:
        return 1.0, position
    ratio, position = _best_window(needle, haystack[start:end])
    return ratio, position + start if position >= 0 else -1


def _needle(field: ExpectedField) -> list[str]:
    return tokenize(normalize_text(field.value, source_html=True))


def _find_unclaimed(
    needle: list[str], haystack: _Haystack, start: int, claimed: list[tuple[int, int]]
) -> int:
    """First verbatim occurrence at or after ``start`` outside every claimed span."""
    position = _find_exact(needle, haystack, start)
    while position >= 0 and any(low <= position < high for low, high in claimed):
        position = _find_exact(needle, haystack, position + 1)
    return position


def _repeat_limit(
    anchor: list[str], others: list[list[str]], haystack: _Haystack, start: int
) -> int:
    """Where an identical copy of this entry's identity starts, else the text end.

    A later occurrence of the anchoring value counts as a repeat only when the
    entry's other identity values sit next to it too (duplicate entries), not
    when a bullet merely mentions the employer again. With no other identity
    value to compare, a repeat cannot be told from a mention: no limit.
    """
    if not others:
        return len(haystack)
    position = _find_exact(anchor, haystack, start + len(anchor))
    while position >= 0:
        low = max(0, position - ENTRY_BACK_SLACK_TOKENS)
        high = position + len(anchor) + ENTRY_BACK_SLACK_TOKENS
        if any(
            _find_exact(other, haystack, low, high + len(other)) >= 0 for other in others
        ):
            return position
        position = _find_exact(anchor, haystack, position + 1)
    return len(haystack)


def _anchor_entries(
    fields: list[ExpectedField], haystack: _Haystack, deadline: float | None = None
) -> dict[str, int]:
    """Walk the text in render order and return each entry's start token.

    ``fields`` are the rendered, visible fields in render order. An entry
    starts at the earliest verbatim occurrence of any of its identity fields
    after the cursor that no earlier entry's own values claim: once an entry
    is anchored, the best match of each of its descriptions (bullets) after
    the anchor is claimed, so the next entry never anchors on an
    employer or title mentioned inside them, even where a two-column layout
    interleaves the bullets with sidebar text. Any field found verbatim within
    ``WALK_MAX_GAP_TOKENS`` of the cursor also moves the cursor past it.
    """
    anchors: dict[str, int] = {}
    limits: dict[str, int] = {}
    claimed: list[tuple[int, int]] = []
    cursor = 0
    for field in fields:
        check_deadline(deadline)
        needle = _needle(field)
        if not needle:
            continue
        entry = field.entry
        if entry is not None and entry not in limits:
            members = [other for other in fields if other.entry == entry]
            identities = [
                identity
                for other in members
                if other.anchor
                for identity in [_needle(other)]
                if identity
            ]
            starts = sorted(
                (position, index)
                for index, identity in enumerate(identities)
                for position in [_find_unclaimed(identity, haystack, cursor, claimed)]
                if position >= 0
            )
            limits[entry] = len(haystack)
            if starts:
                start, index = starts[0]
                others = [
                    identity
                    for other_index, identity in enumerate(identities)
                    if other_index != index and _find_exact(identity, haystack, cursor) >= 0
                ]
                anchors[entry] = cursor = start
                limits[entry] = _repeat_limit(identities[index], others, haystack, start)
                for member in members:
                    check_deadline(deadline)
                    value = _needle(member)
                    if not member.kind.endswith(".description") or not value:
                        continue
                    ratio, position = _locate(value, haystack, start, limits[entry])
                    if ratio == 1.0:
                        claimed.append((position, position + len(value)))
                    elif ratio >= GARBLED_MIN_RATIO:
                        claimed.append((position, position + len(value) + WINDOW_SLACK_TOKENS))
        end = cursor + WALK_MAX_GAP_TOKENS + len(needle)
        if entry is not None:
            end = min(end, limits[entry])
        position = _find_exact(needle, haystack, cursor, end)
        if position >= 0:
            cursor = position + len(needle)
    return anchors


def _entry_spans(
    fields: list[ExpectedField], anchors: dict[str, int], length: int
) -> dict[str, tuple[int, int]]:
    """Token span of each anchored entry: shortly before its anchor to the next one."""
    order = [entry for entry in dict.fromkeys(f.entry for f in fields if f.entry) if entry in anchors]
    spans: dict[str, tuple[int, int]] = {}
    for entry in order:
        start = anchors[entry]
        following = [anchors[other] for other in order if anchors[other] > start]
        spans[entry] = (max(0, start - ENTRY_BACK_SLACK_TOKENS), min(following, default=length))
    return spans


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
    personal_order: tuple[str, ...] | None = None,
    body_order: tuple[str, ...] | None = None,
    deadline: float | None = None,
) -> RoundtripResult:
    """Score how completely and in what order source fields survive extraction.

    Args:
        source: The payload the template rendered (``processed_resume``, with
            section names localized as the print page shows them).
        extracted_text: Text recovered from the rendered file.
        rendered_fields: Field kinds (e.g. ``"workExperience.location"``,
            ``"heading"``) the template prints; others become ``not_rendered``.
            ``None`` means every field is expected.
        personal_order: Render order of personal field kinds, if not the default.
        body_order: Fixed render order of section groups for templates that
            ignore ``sectionMeta`` order; ``None`` keeps ``sectionMeta`` order.
        deadline: ``time.monotonic()`` value after which scoring stops.

    Raises:
        TimeoutError: the deadline passed.
    """
    haystack = _Haystack(tokenize(normalize_text(extracted_text)))
    fields = order_fields(expected_fields(source), personal_order, body_order)
    truncated = len(fields) > MAX_ROUNDTRIP_FIELDS
    fields = fields[:MAX_ROUNDTRIP_FIELDS]
    active = [
        field
        for field in fields
        if not field.hidden and (rendered_fields is None or field.kind in rendered_fields)
    ]
    anchors = _anchor_entries(active, haystack, deadline)
    spans = _entry_spans(active, anchors, len(haystack))
    results: list[RoundtripField] = []
    positions: list[int] = []
    found = considered = 0
    for field in fields:
        check_deadline(deadline)
        status: FieldStatus
        if field.hidden:
            results.append(RoundtripField(field=field.path, status="hidden", score=0.0))
            continue
        if rendered_fields is not None and field.kind not in rendered_fields:
            results.append(RoundtripField(field=field.path, status="not_rendered", score=0.0))
            continue
        needle = _needle(field)
        if not needle:
            continue
        if field.entry in spans:
            # Every value of an entry, including its identity fields, must
            # appear inside that entry, so values swapped between entries or
            # present only in a duplicate entry are not reported as found.
            ratio, position = _locate(needle, haystack, *spans[field.entry])
        else:
            ratio, position = _locate(needle, haystack)
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
        truncated=truncated,
    )
