"""Integration tests for POST /api/v1/resumes/{id}/parse-check (own output).

The real routers run over httpx ASGI against a real temporary database; only
Chromium is replaced: ``render_resume_pdf`` returns the committed real render
of whichever template the PDF route asked for.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import anyio
import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.pdf import (
    RENDER_BUSY_MESSAGE,
    PDFRenderOverloadedError,
    PDFRenderTimeoutError,
)
from app.services.ats_parse import own_output
from app.services.ats_parse.templates import TEMPLATE_IDS

RENDERS = Path(__file__).resolve().parents[1] / "fixtures" / "ats_parse" / "renders"
TWO_COLUMN = {"swiss-two-column", "modern-two-column", "vivid"}
PDF_QUERY_KEYS = {
    "template", "pageSize", "marginTop", "marginBottom", "marginLeft", "marginRight",
    "sectionSpacing", "itemSpacing", "lineHeight", "fontSize", "headerScale",
    "headerFont", "bodyFont", "compactMode", "showContactIcons", "accentColor",
}

Render = Callable[..., Any]


@pytest.fixture
def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(own_output, "RENDER_RETRY_BACKOFF_SECONDS", (0.01, 0.01))


@pytest.fixture
async def resume_id(isolated_db: Any) -> str:
    source = json.loads((RENDERS / "source.json").read_text())
    resume = await isolated_db.create_resume(
        content=json.dumps(source),
        content_type="json",
        processed_data=source,
        processing_status="ready",
    )
    return resume["resume_id"]


def _query(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}


def _fixture_render(
    calls: list[str], fail: Callable[[str, int], Exception | None] | None = None
) -> Render:
    """Serve the committed render of the requested template (lang=es -> es render)."""

    async def render(url: str, page_size: str = "A4", **kwargs: Any) -> bytes:
        query = _query(url)
        calls.append(url)
        attempt = sum(1 for call in calls if _query(call)["template"] == query["template"])
        error = fail(query["template"], attempt) if fail else None
        if error is not None:
            raise error
        stem = "swiss-single-es" if query.get("lang") == "es" else query["template"]
        return (RENDERS / f"{stem}.pdf").read_bytes()

    return render


async def test_single_template_report_with_round_trip(client: AsyncClient, resume_id: str) -> None:
    calls: list[str] = []
    with patch("app.routers.resumes.render_resume_pdf", _fixture_render(calls)):
        async with client:
            response = await client.post(f"/api/v1/resumes/{resume_id}/parse-check", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["resume_id"] == resume_id
    assert body["render_locale"] == "en"
    [result] = body["results"]
    assert result["template"] == "swiss-single"
    assert result["status"] == "ok"
    assert result["expected_by_template"] is False
    assert result["render_attempts"] == 1
    report = result["report"]
    assert report["content_language"] == "en"  # the configured content language
    assert report["roundtrip"]["content_recall"] >= 0.95
    assert report["roundtrip"]["order_fidelity"] >= 0.95
    # The PDF route received every template setting (own output = user download).
    assert set(_query(calls[0])) == PDF_QUERY_KEYS
    assert _query(calls[0])["template"] == "swiss-single"


async def test_full_settings_reach_the_pdf_route(client: AsyncClient, resume_id: str) -> None:
    settings = {
        "template": "latex",
        "pageSize": "LETTER",
        "margins": {"top": 12, "bottom": 14, "left": 16, "right": 18},
        "spacing": {"section": 4, "item": 1, "lineHeight": 5},
        "fontSize": {"base": 2, "headerScale": 4, "headerFont": "mono", "bodyFont": "serif"},
        "compactMode": True,
        "showContactIcons": True,
        "accentColor": "red",
        "lang": "es",
    }
    calls: list[str] = []
    with patch("app.routers.resumes.render_resume_pdf", _fixture_render(calls)):
        async with client:
            response = await client.post(
                f"/api/v1/resumes/{resume_id}/parse-check", json={"settings": settings}
            )
    assert response.status_code == 200
    assert _query(calls[0]) == {
        "template": "latex", "pageSize": "LETTER", "marginTop": "12", "marginBottom": "14",
        "marginLeft": "16", "marginRight": "18", "sectionSpacing": "4", "itemSpacing": "1",
        "lineHeight": "5", "fontSize": "2", "headerScale": "4", "headerFont": "mono",
        "bodyFont": "serif", "compactMode": "true", "showContactIcons": "true",
        "accentColor": "red", "lang": "es",
    }
    body = response.json()
    assert body["render_locale"] == "es"
    checks = {check["id"]: check for check in body["results"][0]["report"]["checks"]}
    assert checks["section_headings"]["params"]["render_locale"] == "es"


async def test_all_templates_reports_every_template_in_order(
    client: AsyncClient, resume_id: str
) -> None:
    calls: list[str] = []
    with patch("app.routers.resumes.render_resume_pdf", _fixture_render(calls)):
        async with client:
            response = await client.post(
                f"/api/v1/resumes/{resume_id}/parse-check",
                json={"all_templates": True, "settings": {"template": "vivid"}},
            )
    assert response.status_code == 200
    results = response.json()["results"]
    assert [result["template"] for result in results] == list(TEMPLATE_IDS)
    for result in results:
        assert result["status"] == "ok"
        assert result["expected_by_template"] is (result["template"] in TWO_COLUMN)
        checks = {check["id"]: check for check in result["report"]["checks"]}
        multi_column = checks["multi_column"]
        if result["template"] in TWO_COLUMN:
            assert multi_column["status"] == "fail"
            assert multi_column["params"]["expected_by_template"] is True
        else:
            assert multi_column["status"] == "pass"


async def test_overloaded_template_is_render_failed_and_others_are_reported(
    client: AsyncClient, resume_id: str
) -> None:
    def overload_modern(template: str, attempt: int) -> Exception | None:
        return PDFRenderOverloadedError(RENDER_BUSY_MESSAGE) if template == "modern" else None

    calls: list[str] = []
    with patch("app.routers.resumes.render_resume_pdf", _fixture_render(calls, overload_modern)):
        async with client:
            response = await client.post(
                f"/api/v1/resumes/{resume_id}/parse-check", json={"all_templates": True}
            )
    assert response.status_code == 200
    results = {result["template"]: result for result in response.json()["results"]}
    assert results["modern"] == {
        "template": "modern",
        "status": "render_failed",
        "expected_by_template": False,
        "render_attempts": own_output.MAX_RENDER_ATTEMPTS,
        "error": "render_busy",
        "report": None,
    }
    others = [result for template, result in results.items() if template != "modern"]
    assert len(others) == 6
    assert all(result["status"] == "ok" and result["report"] for result in others)


async def test_busy_renderer_is_retried_until_a_slot_frees(
    client: AsyncClient, resume_id: str
) -> None:
    def busy_once(template: str, attempt: int) -> Exception | None:
        return PDFRenderOverloadedError(RENDER_BUSY_MESSAGE) if attempt == 1 else None

    calls: list[str] = []
    with patch("app.routers.resumes.render_resume_pdf", _fixture_render(calls, busy_once)):
        async with client:
            response = await client.post(f"/api/v1/resumes/{resume_id}/parse-check", json={})
    assert response.status_code == 200
    [result] = response.json()["results"]
    assert result["status"] == "ok"
    assert result["render_attempts"] == 2


@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (PDFRenderOverloadedError(RENDER_BUSY_MESSAGE), 503),
        (PDFRenderTimeoutError("PDF rendering timed out. Please try again, or try a simpler resume."), 504),
    ],
)
async def test_single_template_render_failure_is_an_http_error(
    client: AsyncClient, resume_id: str, error: Exception, status_code: int
) -> None:
    calls: list[str] = []
    with patch(
        "app.routers.resumes.render_resume_pdf", _fixture_render(calls, lambda *_: error)
    ):
        async with client:
            response = await client.post(f"/api/v1/resumes/{resume_id}/parse-check", json={})
    assert response.status_code == status_code
    assert "Traceback" not in response.text
    # Only a busy renderer is retried; a timeout is not.
    expected_attempts = own_output.MAX_RENDER_ATTEMPTS if status_code == 503 else 1
    assert len(calls) == expected_attempts


async def test_budget_exhaustion_times_out_remaining_templates(
    client: AsyncClient, resume_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(own_output, "MIN_ANALYSIS_SECONDS", 0.0)
    monkeypatch.setattr(own_output, "ALL_TEMPLATES_BUDGET_SECONDS", 1.5)
    fixture_render = _fixture_render([])

    async def slow_after_first(url: str, *args: Any, **kwargs: Any) -> bytes:
        if _query(url)["template"] != "swiss-single":
            await anyio.sleep(5)
        return await fixture_render(url, *args, **kwargs)

    with patch("app.routers.resumes.render_resume_pdf", slow_after_first):
        async with client:
            response = await client.post(
                f"/api/v1/resumes/{resume_id}/parse-check", json={"all_templates": True}
            )
    assert response.status_code == 200
    results = response.json()["results"]
    assert results[0]["status"] == "ok"
    assert {result["status"] for result in results[1:]} == {"timed_out"}
    assert {result["error"] for result in results[1:]} == {"budget_exhausted"}


async def test_unknown_resume_is_404(client: AsyncClient, isolated_db: Any) -> None:
    async with client:
        missing = await client.post("/api/v1/resumes/does-not-exist/parse-check", json={})
        unsafe = await client.post("/api/v1/resumes/..%2Fconfig/parse-check", json={})
    assert missing.status_code == 404
    assert unsafe.status_code == 404


async def test_unprocessed_resume_is_409(client: AsyncClient, isolated_db: Any) -> None:
    resume = await isolated_db.create_resume(content="# Draft", processing_status="processing")
    async with client:
        response = await client.post(
            f"/api/v1/resumes/{resume['resume_id']}/parse-check", json={}
        )
    assert response.status_code == 409


@pytest.mark.parametrize(
    "body",
    [
        {"settings": {"margins": {"top": 30}}},
        {"settings": {"template": "fancy"}},
        {"settings": {"lang": "xx"}},
        {"settings": {"unknown": True}},
        {"content_language": "xx"},
        {"all": True},
    ],
)
async def test_invalid_body_is_422(client: AsyncClient, resume_id: str, body: dict[str, Any]) -> None:
    async with client:
        response = await client.post(f"/api/v1/resumes/{resume_id}/parse-check", json=body)
    assert response.status_code == 422


async def test_missing_body_uses_default_settings(client: AsyncClient, resume_id: str) -> None:
    calls: list[str] = []
    with patch("app.routers.resumes.render_resume_pdf", _fixture_render(calls)):
        async with client:
            response = await client.post(f"/api/v1/resumes/{resume_id}/parse-check")
    assert response.status_code == 200
    assert response.json()["settings"]["template"] == "swiss-single"
    assert "lang" not in _query(calls[0])


async def test_same_request_returns_byte_identical_body(
    client: AsyncClient, resume_id: str
) -> None:
    with patch("app.routers.resumes.render_resume_pdf", _fixture_render([])):
        async with client:
            first = await client.post(f"/api/v1/resumes/{resume_id}/parse-check", json={})
            second = await client.post(f"/api/v1/resumes/{resume_id}/parse-check", json={})
    assert first.status_code == second.status_code == 200
    assert first.content == second.content
