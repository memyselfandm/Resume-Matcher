"""Round-trip self-consistency tests (pure function over source + extracted text)."""

import copy
import json
import time
from pathlib import Path
from typing import Any

import pytest

from app.services.ats_parse.extract import MAX_EXTRACTED_CHARS, extract_document
from app.services.ats_parse.roundtrip import (
    MAX_ROUNDTRIP_FIELDS,
    compute_roundtrip,
    expected_fields,
    order_fields,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ats_parse"


@pytest.fixture(scope="module")
def source() -> dict[str, Any]:
    return json.loads((FIXTURES / "swiss_single_like.source.json").read_text())


@pytest.fixture(scope="module")
def rendered_text() -> str:
    return extract_document((FIXTURES / "swiss_single_like.pdf").read_bytes(), "x.pdf").text


def _statuses(result: Any) -> dict[str, str]:
    return {field.field: field.status for field in result.fields}


def test_clean_render_round_trips(source: dict[str, Any], rendered_text: str) -> None:
    result = compute_roundtrip(source, rendered_text)
    assert result.content_recall >= 0.95
    assert result.order_fidelity >= 0.95
    statuses = _statuses(result)
    for field in ("name", "email", "phone", "linkedin", "location"):
        assert statuses[f"personalInfo.{field}"] == "found"


def test_uppercase_rendered_heading_is_found(source: dict[str, Any], rendered_text: str) -> None:
    assert "EXPERIENCE" in rendered_text
    statuses = _statuses(compute_roundtrip(source, rendered_text))
    assert statuses["heading.workExperience"] == "found"
    assert statuses["heading.education"] == "found"


def test_corrupted_fields_and_only_those_are_flagged(
    source: dict[str, Any], rendered_text: str
) -> None:
    corrupted = rendered_text.replace("Jordan Rivera".upper(), "")
    corrupted = corrupted.replace("Jun 2017 - Dec 2020", "(cid:41)(cid:42)")
    corrupted = corrupted.replace(
        "Improved query latency by 60% by redesigning the reporting schema.",
        "Improved query latency by 60% xx yy zz qq.",
    )
    result = compute_roundtrip(source, corrupted)
    flagged = {
        field.field: field.status for field in result.fields if field.status != "found"
    }
    assert flagged == {
        "personalInfo.name": "missing",
        "workExperience[1].years": "missing",
        "workExperience[1].description[1]": "garbled",
    }
    assert result.content_recall < 0.95


def test_hidden_section_is_not_reported_missing(source: dict[str, Any], rendered_text: str) -> None:
    hidden = copy.deepcopy(source)
    for entry in hidden["sectionMeta"]:
        if entry["key"] == "education":
            entry["isVisible"] = False
    before, rest = rendered_text.split("EDUCATION", 1)
    text_without_education = before + "SKILLS" + rest.split("SKILLS", 1)[1]
    result = compute_roundtrip(hidden, text_without_education)
    statuses = _statuses(result)
    education = {path: status for path, status in statuses.items() if "education" in path}
    assert education and set(education.values()) == {"hidden"}
    assert "missing" not in statuses.values()


def test_fields_outside_rendered_map_are_not_rendered(
    source: dict[str, Any], rendered_text: str
) -> None:
    without_locations = rendered_text
    for job in source["workExperience"]:
        without_locations = without_locations.replace(job["location"], "")
    all_kinds = {field.kind for field in expected_fields(source)}
    rendered = frozenset(all_kinds - {"workExperience.location"})
    result = compute_roundtrip(source, without_locations, rendered_fields=rendered)
    statuses = _statuses(result)
    assert statuses["workExperience[0].location"] == "not_rendered"
    assert "missing" not in statuses.values()


def test_swapped_sections_lower_order_fidelity(source: dict[str, Any], rendered_text: str) -> None:
    head, rest = rendered_text.split("EXPERIENCE", 1)
    experience, tail = rest.split("EDUCATION", 1)
    swapped = head + "EDUCATION" + tail + "\nEXPERIENCE" + experience
    result = compute_roundtrip(source, swapped)
    assert result.content_recall >= 0.95
    assert result.order_fidelity < 0.9


def test_html_bullets_match_plain_extracted_text() -> None:
    source = {
        "personalInfo": {"name": "Ada Example"},
        "workExperience": [
            {"title": "Engineer", "company": "Acme", "years": "2020 - 2022",
             "description": ["<strong>Built</strong> the data&nbsp;pipeline &amp; tooling"]},
        ],
    }
    text = "Ada Example\nEngineer 2020 - 2022\nAcme\n\u2022 Built the data pipeline & tooling"
    result = compute_roundtrip(source, text)
    assert {field.status for field in result.fields} == {"found"}
    assert result.content_recall == 1.0


def test_swapped_dates_between_entries_are_flagged(
    source: dict[str, Any], rendered_text: str
) -> None:
    """Each entry's date must appear inside that entry, not anywhere in the text."""
    first, second = "Jan 2021 - Present", "Jun 2017 - Dec 2020"
    assert first in rendered_text and second in rendered_text
    swapped = (
        rendered_text.replace(first, "\x00").replace(second, first).replace("\x00", second)
    )
    result = compute_roundtrip(source, swapped)
    flagged = {field.field: field.status for field in result.fields if field.status != "found"}
    assert set(flagged) == {"workExperience[0].years", "workExperience[1].years"}
    assert set(flagged.values()) <= {"missing", "garbled"}
    assert result.content_recall < 0.97


def test_substring_titles_anchor_to_their_own_entry(source: dict[str, Any], rendered_text: str) -> None:
    """'Software Engineer' must not anchor inside 'Senior Software Engineer'."""
    result = compute_roundtrip(source, rendered_text)
    statuses = _statuses(result)
    assert statuses["workExperience[1].title"] == "found"
    assert statuses["workExperience[1].years"] == "found"


@pytest.mark.parametrize(
    ("titles", "bullets"),
    [
        (("Senior Engineer", "Engineer"), ("Built search ranking pipelines.", "Maintained index services.")),
        (("Engineer", "Engineer"), ("Built search ranking pipelines.", "Maintained index services.")),
    ],
)
def test_repeated_employer_entries_each_anchor_to_their_own_text(
    titles: tuple[str, str], bullets: tuple[str, str]
) -> None:
    years = ("2021 - 2023", "2018 - 2021")
    source = {
        "personalInfo": {"name": "Ada Example"},
        "workExperience": [
            {"title": title, "company": "Google", "years": year, "description": [bullet]}
            for title, year, bullet in zip(titles, years, bullets, strict=True)
        ],
    }
    text = "Ada Example\n" + "\n".join(
        f"{title} {year}\nGoogle\n{bullet}"
        for title, year, bullet in zip(titles, years, bullets, strict=True)
    )
    result = compute_roundtrip(source, text)
    assert result.content_recall == 1.0
    assert {field.status for field in result.fields} == {"found"}


def _two_entry_source(first_bullets: list[str]) -> dict[str, Any]:
    return {
        "personalInfo": {"name": "Ada Example"},
        "workExperience": [
            {"title": "Senior Engineer", "company": "Google", "years": "2021 - 2023",
             "description": first_bullets},
            {"title": "Engineer", "company": "Google", "years": "2018 - 2021",
             "description": ["Maintained index services."]},
        ],
    }


def _two_entry_text(first_bullets: list[str]) -> str:
    return (
        "Ada Example\nSenior Engineer 2021 - 2023\nGoogle\n"
        + "\n".join(first_bullets)
        + "\nEngineer 2018 - 2021\nGoogle\nMaintained index services."
    )


@pytest.mark.parametrize(
    "bullets",
    [
        # The next entry's employer inside the last bullet.
        ["Built search ranking pipelines.", "Shipped features for Google Maps"],
        # The next entry's title and employer at the start of a bullet.
        ["Engineer tooling for Google teams", "Built search ranking pipelines."],
    ],
)
def test_bullet_naming_next_entry_does_not_move_its_anchor(bullets: list[str]) -> None:
    result = compute_roundtrip(_two_entry_source(bullets), _two_entry_text(bullets))
    assert {field.status for field in result.fields} == {"found"}
    assert result.content_recall == 1.0


def test_bullet_naming_next_entry_in_company_first_layout() -> None:
    """Templates such as latex print the company before the title."""
    bullets = ["Built search ranking pipelines.", "Shipped features for Google Maps"]
    text = (
        "Ada Example\nGoogle 2021 - 2023\nSenior Engineer\n"
        + "\n".join(bullets)
        + "\nGoogle 2018 - 2021\nEngineer\nMaintained index services."
    )
    result = compute_roundtrip(_two_entry_source(bullets), text)
    assert {field.status for field in result.fields} == {"found"}


def test_missing_company_is_not_masked_by_an_identical_entry() -> None:
    source = {
        "personalInfo": {"name": "Ada Example"},
        "workExperience": [
            {"title": "Engineer", "company": "Google", "years": year,
             "description": ["Built search ranking pipelines."]}
            for year in ("2021 - 2023", "2018 - 2021")
        ],
    }
    text = (
        "Ada Example\nEngineer 2021 - 2023\nBuilt search ranking pipelines.\n"
        "Engineer 2018 - 2021\nGoogle\nBuilt search ranking pipelines."
    )
    flagged = {
        field.field: field.status
        for field in compute_roundtrip(source, text).fields
        if field.status != "found"
    }
    assert flagged == {"workExperience[0].company": "missing"}


def test_fixed_layout_order_is_the_template_order(source: dict[str, Any]) -> None:
    """Two-column templates print education in the sidebar, after the main column."""
    fields = order_fields(
        expected_fields(source),
        body_order=("summary", "workExperience", "additional.technicalSkills", "education"),
    )
    groups = list(dict.fromkeys(field.group for field in fields))
    assert groups.index("additional.technicalSkills") < groups.index("education")
    assert groups[0].startswith("personalInfo.")


def test_custom_item_kinds_do_not_include_the_section_key() -> None:
    source = {
        "sectionMeta": [
            {"id": "pubs", "key": "pubs", "displayName": "Publications", "isVisible": True, "order": 1}
        ],
        "customSections": {"pubs": {"sectionType": "itemList", "items": [{"title": "Paper"}]}},
    }
    kinds = {field.path: field.kind for field in expected_fields(source)}
    assert kinds["customSections.pubs[0].title"] == "customSections.title"


def _huge_source(bullets: int) -> tuple[dict[str, Any], str]:
    descriptions = [
        f"Delivered project {index} for client team {index * 7} with outcome {index * 3} percent"
        for index in range(bullets)
    ]
    source = {
        "personalInfo": {"name": "Ada Example"},
        "workExperience": [
            {"title": "Engineer", "company": "Acme", "years": "2020 - 2024",
             "description": descriptions}
        ],
    }
    text = "Ada Example\nEngineer Acme 2020 - 2024\n" + "\n".join(
        line if index % 2 else "unrelated words" for index, line in enumerate(descriptions)
    )
    return source, text[:MAX_EXTRACTED_CHARS]


def test_huge_source_is_capped_and_marked_truncated() -> None:
    source, text = _huge_source(5_000)
    started = time.monotonic()
    result = compute_roundtrip(source, text, deadline=started + 60)
    assert result.truncated is True
    assert len(result.fields) == MAX_ROUNDTRIP_FIELDS
    assert time.monotonic() - started < 30


def test_small_source_is_not_truncated(source: dict[str, Any], rendered_text: str) -> None:
    assert compute_roundtrip(source, rendered_text).truncated is False


def test_round_trip_stops_at_the_deadline() -> None:
    source, text = _huge_source(5_000)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        compute_roundtrip(source, text, deadline=started + 0.2)
    # The deadline is checked per field, including inside an entry's bullets.
    assert time.monotonic() - started < 1.5


def _custom_source(**meta_overrides: Any) -> dict[str, Any]:
    """Three visible custom sections, one of each type, as the builder saves them."""
    meta = [
        {"id": "summary", "key": "summary", "displayName": "Summary", "sectionType": "text",
         "isDefault": True, "isVisible": True, "order": 1},
        {"id": "custom_1", "key": "custom_1", "displayName": "Volunteering",
         "sectionType": "stringList", "isDefault": False, "isVisible": True, "order": 2},
        {"id": "custom_2", "key": "custom_2", "displayName": "Interests",
         "sectionType": "text", "isDefault": False, "isVisible": True, "order": 3},
        {"id": "custom_3", "key": "custom_3", "displayName": "Talks",
         "sectionType": "itemList", "isDefault": False, "isVisible": True, "order": 4},
    ]
    for entry in meta:
        entry.update(meta_overrides.get(entry["key"], {}))
    return {
        "personalInfo": {"name": "Ada Example"},
        "summary": "Backend engineer building data platforms.",
        "sectionMeta": meta,
        "customSections": {
            "custom_1": {"sectionType": "stringList",
                         "strings": ["Code mentor at Oakland Youth Coding Club", "Food bank driver"]},
            "custom_2": {"sectionType": "text", "text": "Trail running and amateur radio."},
            "custom_3": {"sectionType": "itemList", "items": [
                {"id": 1, "title": "Scaling Event Pipelines", "subtitle": "PyBay",
                 "years": "2024", "description": ["Talk on exactly-once delivery."]}
            ]},
        },
    }


@pytest.mark.parametrize("joiner", [", ", " • "])
def test_rendered_custom_sections_of_every_type_are_found(joiner: str) -> None:
    """String lists print joined (", " in most templates, " • " in vivid)."""
    text = (
        "Ada Example\nSummary\nBackend engineer building data platforms.\n"
        f"Volunteering\nCode mentor at Oakland Youth Coding Club{joiner}Food bank driver\n"
        "Interests\nTrail running and amateur radio.\n"
        "Talks\nScaling Event Pipelines 2024\nPyBay\nTalk on exactly-once delivery."
    )
    statuses = _statuses(compute_roundtrip(_custom_source(), text))
    custom = {path: status for path, status in statuses.items() if "custom_" in path}
    assert custom == {
        "heading.custom_1": "found",
        "customSections.custom_1.strings[0]": "found",
        "customSections.custom_1.strings[1]": "found",
        "heading.custom_2": "found",
        "customSections.custom_2.text": "found",
        "heading.custom_3": "found",
        "customSections.custom_3[0].title": "found",
        "customSections.custom_3[0].subtitle": "found",
        "customSections.custom_3[0].years": "found",
        "customSections.custom_3[0].description[0]": "found",
    }


def test_hidden_custom_sections_are_hidden() -> None:
    source = _custom_source(
        custom_1={"isVisible": False}, custom_2={"isVisible": False}, custom_3={"isVisible": False}
    )
    result = compute_roundtrip(source, "Ada Example\nSummary\nBackend engineer building data platforms.")
    statuses = _statuses(result)
    assert {status for path, status in statuses.items() if "custom_" in path} == {"hidden"}
    assert result.content_recall == 1.0


def test_custom_section_with_default_flag_is_not_rendered() -> None:
    """Templates print a custom section only when ``isDefault`` is false; a meta
    saved without the flag gets the backend default (true) and is skipped."""
    source = _custom_source(custom_1={"isDefault": True})
    text = (
        "Ada Example\nSummary\nBackend engineer building data platforms.\n"
        "Interests\nTrail running and amateur radio.\n"
        "Talks\nScaling Event Pipelines 2024\nPyBay\nTalk on exactly-once delivery."
    )
    result = compute_roundtrip(source, text)
    statuses = _statuses(result)
    assert statuses["heading.custom_1"] == "not_rendered"
    assert statuses["customSections.custom_1.strings[0]"] == "not_rendered"
    assert statuses["customSections.custom_2.text"] == "found"
    assert result.content_recall == 1.0


def test_custom_section_prints_only_the_content_of_its_meta_type() -> None:
    source = _custom_source(custom_1={"sectionType": "text"})
    text = (
        "Ada Example\nSummary\nBackend engineer building data platforms.\n"
        "Interests\nTrail running and amateur radio.\n"
        "Talks\nScaling Event Pipelines 2024\nPyBay\nTalk on exactly-once delivery."
    )
    statuses = _statuses(compute_roundtrip(source, text))
    # A "text" section with only strings has no content to print, not even its heading.
    assert statuses["heading.custom_1"] == "not_rendered"
    assert statuses["customSections.custom_1.strings[1]"] == "not_rendered"
