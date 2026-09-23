"""Round-trip self-consistency tests (pure function over source + extracted text)."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from app.services.ats_parse.extract import extract_document
from app.services.ats_parse.roundtrip import compute_roundtrip, expected_fields

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
