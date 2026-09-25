"""Integration tests for POST /api/v1/resumes/{id}/parse-check (own output).

The real routers run over httpx ASGI against a real temporary database; only
Chromium is replaced: ``render_resume_pdf`` returns the committed real render
of whichever template the PDF route asked for.
"""

import json
from collections.abc import Callable
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import anyio
import pytest
from httpx import ASGITransport, AsyncClient

from app import pdf
from app.main import app
from app.pdf import (
    RENDER_BUSY_MESSAGE,
    PDFRenderOverloadedError,
    PDFRenderTimeoutError,
)
from app.services.ats_parse import own_output
from app.services.ats_parse.engine import PARSE_CHECK_TIMEOUT_SECONDS
from app.services.ats_parse.roundtrip import MAX_ROUNDTRIP_FIELDS
from app.services.ats_parse.templates import TEMPLATE_IDS
from tests.ats_parse_renders import render_pdf, source as render_source

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
    source = render_source()
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
        return render_pdf(stem)

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


async def test_analysis_error_of_one_template_does_not_stop_the_sweep(
    client: AsyncClient, resume_id: str
) -> None:
    fixture_render = _fixture_render([])

    async def unreadable_modern(url: str, *args: Any, **kwargs: Any) -> bytes:
        if _query(url)["template"] == "modern":
            return b"%PDF-1.7 not really a pdf"
        return await fixture_render(url, *args, **kwargs)

    with patch("app.routers.resumes.render_resume_pdf", unreadable_modern):
        async with client:
            response = await client.post(
                f"/api/v1/resumes/{resume_id}/parse-check", json={"all_templates": True}
            )
            single = await client.post(
                f"/api/v1/resumes/{resume_id}/parse-check",
                json={"settings": {"template": "modern"}},
            )
    assert response.status_code == 200
    results = {result["template"]: result for result in response.json()["results"]}
    assert results["modern"] == {
        "template": "modern",
        "status": "render_failed",
        "expected_by_template": False,
        "render_attempts": 1,
        "error": "analysis_error",
        "report": None,
    }
    assert all(
        result["status"] == "ok" for template, result in results.items() if template != "modern"
    )
    assert single.status_code == 500
    assert "Traceback" not in single.text


async def test_portuguese_render_locale_is_accepted(client: AsyncClient, resume_id: str) -> None:
    calls: list[str] = []
    with patch("app.routers.resumes.render_resume_pdf", _fixture_render(calls)):
        async with client:
            response = await client.post(
                f"/api/v1/resumes/{resume_id}/parse-check", json={"settings": {"lang": "pt"}}
            )
            regional = await client.post(
                f"/api/v1/resumes/{resume_id}/parse-check", json={"settings": {"lang": "pt-BR"}}
            )
    assert response.status_code == 200
    assert response.json()["render_locale"] == "pt"
    assert _query(calls[0])["lang"] == "pt"
    checks = {check["id"]: check for check in response.json()["results"][0]["report"]["checks"]}
    assert checks["section_headings"]["params"]["render_locale"] == "pt"
    # The frontend only knows "pt"; "pt-BR" would silently render in English.
    assert regional.status_code == 422


async def test_huge_resume_round_trip_is_capped_within_the_budget(
    client: AsyncClient, isolated_db: Any
) -> None:
    source = render_source()
    source["workExperience"][0]["description"] = [
        f"Delivered project {index} for client team {index * 7}" for index in range(3_000)
    ]
    resume = await isolated_db.create_resume(
        content=json.dumps(source),
        content_type="json",
        processed_data=source,
        processing_status="ready",
    )
    with patch("app.routers.resumes.render_resume_pdf", _fixture_render([])):
        async with client:
            response = await client.post(
                f"/api/v1/resumes/{resume['resume_id']}/parse-check", json={}
            )
    assert response.status_code == 200
    [result] = response.json()["results"]
    assert result["status"] == "ok"
    roundtrip = result["report"]["roundtrip"]
    assert roundtrip["truncated"] is True
    assert len(roundtrip["fields"]) == MAX_ROUNDTRIP_FIELDS


async def test_concurrent_own_output_check_is_429(client: AsyncClient, resume_id: str) -> None:
    started = anyio.Event()
    release = anyio.Event()
    fixture_render = _fixture_render([])

    async def blocking_render(url: str, *args: Any, **kwargs: Any) -> bytes:
        started.set()
        await release.wait()
        return await fixture_render(url, *args, **kwargs)

    responses: dict[str, Any] = {}

    async def first_check() -> None:
        responses["first"] = await client.post(
            f"/api/v1/resumes/{resume_id}/parse-check", json={}
        )

    with patch("app.routers.resumes.render_resume_pdf", blocking_render):
        async with client:
            async with anyio.create_task_group() as group:
                group.start_soon(first_check)
                await started.wait()
                responses["second"] = await client.post(
                    f"/api/v1/resumes/{resume_id}/parse-check", json={"all_templates": True}
                )
                release.set()
            responses["after"] = await client.post(
                f"/api/v1/resumes/{resume_id}/parse-check", json={}
            )
    assert responses["second"].status_code == 429
    assert responses["second"].headers["Retry-After"] == str(own_output.BUSY_RETRY_AFTER_SECONDS)
    assert responses["first"].status_code == 200
    assert responses["after"].status_code == 200  # the slot is released


async def test_single_renderer_slot_is_polled_until_the_budget_runs_out(
    client: AsyncClient, resume_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pdf, "_PDF_MAX_CONCURRENCY", 1)
    monkeypatch.setattr(own_output, "SINGLE_SLOT_POLL_SECONDS", 0.05)
    monkeypatch.setattr(own_output, "MIN_ANALYSIS_SECONDS", 0.2)
    monkeypatch.setattr(own_output, "SINGLE_TEMPLATE_BUDGET_SECONDS", 1.0)
    calls: list[str] = []
    always_busy = _fixture_render(calls, lambda *_: PDFRenderOverloadedError(RENDER_BUSY_MESSAGE))
    with patch("app.routers.resumes.render_resume_pdf", always_busy):
        async with client:
            response = await client.post(f"/api/v1/resumes/{resume_id}/parse-check", json={})
    assert response.status_code == 503
    # Polled past the three attempts a shared renderer gets.
    assert len(calls) > own_output.MAX_RENDER_ATTEMPTS


async def test_sweep_waits_for_a_user_download_holding_the_only_slot(
    client: AsyncClient, resume_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PDF_MAX_CONCURRENCY=1 and a user render in progress when the sweep starts."""
    monkeypatch.setattr(pdf, "_PDF_MAX_CONCURRENCY", 1)
    monkeypatch.setattr(pdf, "_last_download_refusal", None)
    monkeypatch.setattr(pdf, "_browser_is_connected", lambda browser: True)
    monkeypatch.setattr(own_output, "SINGLE_SLOT_POLL_SECONDS", 0.05)
    user_rendering = anyio.Event()

    async def shared_browser_render(url: str, *args: Any) -> bytes:
        if not pdf._background_render.get():
            user_rendering.set()
            await anyio.sleep(0.5)
        return render_pdf(_query(url)["template"])

    monkeypatch.setattr(pdf, "_render_on_shared_browser", shared_browser_render)
    responses: dict[str, Any] = {}

    async def download() -> None:
        responses["user"] = await client.get(f"/api/v1/resumes/{resume_id}/pdf")

    async with client:
        async with anyio.create_task_group() as group:
            group.start_soon(download)
            await user_rendering.wait()
            responses["sweep"] = await client.post(
                f"/api/v1/resumes/{resume_id}/parse-check", json={"all_templates": True}
            )

    assert responses["user"].status_code == 200
    assert responses["sweep"].status_code == 200
    results = responses["sweep"].json()["results"]
    assert [result["status"] for result in results] == ["ok"] * len(TEMPLATE_IDS)
    assert results[0]["render_attempts"] > 1  # waited for the user's render


async def test_user_download_refused_during_a_sweep_gets_the_slot_on_retry(
    client: AsyncClient, resume_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PDF_MAX_CONCURRENCY=1: the sweep yields the renderer to a refused user."""
    monkeypatch.setattr(pdf, "_PDF_MAX_CONCURRENCY", 1)
    monkeypatch.setattr(pdf, "_last_download_refusal", None)
    monkeypatch.setattr(pdf, "_browser_is_connected", lambda browser: True)
    monkeypatch.setattr(own_output, "DOWNLOAD_PRIORITY_SECONDS", 1.0)
    first_sweep_render = anyio.Event()
    release_first = anyio.Event()
    first_done = anyio.Event()
    timeline: list[tuple[str, str, float, float]] = []

    async def shared_browser_render(url: str, *args: Any) -> bytes:
        who = "sweep" if pdf._background_render.get() else "user"
        template = _query(url)["template"]
        start = anyio.current_time()
        if who == "sweep" and not first_sweep_render.is_set():
            first_sweep_render.set()
            await release_first.wait()
            first_done.set()
        else:
            await anyio.sleep(0.05)
        timeline.append((who, template, start, anyio.current_time()))
        return render_pdf(template)

    monkeypatch.setattr(pdf, "_render_on_shared_browser", shared_browser_render)
    responses: dict[str, Any] = {}

    async def sweep() -> None:
        responses["sweep"] = await client.post(
            f"/api/v1/resumes/{resume_id}/parse-check", json={"all_templates": True}
        )

    async with client:
        async with anyio.create_task_group() as group:
            group.start_soon(sweep)
            await first_sweep_render.wait()
            responses["refused"] = await client.get(f"/api/v1/resumes/{resume_id}/pdf")
            release_first.set()
            await first_done.wait()
            responses["retried"] = await client.get(f"/api/v1/resumes/{resume_id}/pdf")

    assert responses["refused"].status_code == 503
    assert responses["retried"].status_code == 200
    assert responses["sweep"].status_code == 200
    assert {result["status"] for result in responses["sweep"].json()["results"]} == {"ok"}
    [user_render] = [entry for entry in timeline if entry[0] == "user"]
    later_sweep_renders = [entry for entry in timeline if entry[0] == "sweep"][1:]
    assert len(later_sweep_renders) == len(TEMPLATE_IDS) - 1
    assert all(entry[2] >= user_render[3] for entry in later_sweep_renders)


async def test_extraction_timeout_is_bounded_by_budget_and_engine_limit(
    client: AsyncClient, resume_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    timeouts: list[float] = []
    real_run_parse_check = own_output.run_parse_check

    async def spy(*args: Any, timeout_seconds: float, **kwargs: Any) -> Any:
        timeouts.append(timeout_seconds)
        return await real_run_parse_check(*args, timeout_seconds=timeout_seconds, **kwargs)

    monkeypatch.setattr(own_output, "run_parse_check", spy)
    with patch("app.routers.resumes.render_resume_pdf", _fixture_render([])):
        async with client:
            response = await client.post(
                f"/api/v1/resumes/{resume_id}/parse-check", json={"all_templates": True}
            )
    assert response.status_code == 200
    assert len(timeouts) == len(TEMPLATE_IDS)
    # The 200 s sweep budget never grants one extraction more than the engine limit.
    assert max(timeouts) <= PARSE_CHECK_TIMEOUT_SECONDS


async def test_extraction_is_not_started_with_under_a_second_left(
    client: AsyncClient, resume_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(own_output, "MIN_ANALYSIS_SECONDS", 0.0)
    monkeypatch.setattr(own_output, "SINGLE_TEMPLATE_BUDGET_SECONDS", 1.2)
    started: list[bool] = []

    async def never_called(*args: Any, **kwargs: Any) -> Any:
        started.append(True)
        raise AssertionError("extraction started")

    monkeypatch.setattr(own_output, "run_parse_check", never_called)
    fixture_render = _fixture_render([])

    async def slow_render(url: str, *args: Any, **kwargs: Any) -> bytes:
        await anyio.sleep(0.5)
        return await fixture_render(url, *args, **kwargs)

    with patch("app.routers.resumes.render_resume_pdf", slow_render):
        async with client:
            response = await client.post(f"/api/v1/resumes/{resume_id}/parse-check", json={})
    assert response.status_code == 504
    assert started == []
