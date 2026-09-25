"""MCP ATS tools through the SDK's in-process client.

The real routers run against the per-test isolated database. LLM services are
replaced at the router boundary (``mocked_tailoring``) and Chromium is
replaced by the committed real renders: ``render_resume_pdf`` returns the
render of whichever template the PDF route asked for, so the own-output path
(PDF route, extraction, round trip) runs for real.
"""

import asyncio
import base64
import copy
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest

from app.database import Database
from app.main import app
from app.mcp.bridge import MAX_UPLOAD_BYTES
from app.mcp.tools import ats
from app.schemas.models import ResumeData
from app.services.ats_parse import own_output
from app.services.ats_parse.templates import TEMPLATE_IDS
from tests.ats_parse_renders import render_pdf, source as render_source
from tests.integration.test_mcp_server import (
    RecordingApp,
    error_text,
    mcp_session,
    mocked_tailoring,
    payload,
    poll_task,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ats_parse"
JOB_DESCRIPTION = "Senior Backend Engineer: Python, FastAPI, PostgreSQL, AWS."
SUMMARY_LIMIT_BYTES = 2048
TWO_COLUMN_TEMPLATES = {"swiss-two-column", "modern-two-column", "vivid"}

Render = Callable[..., Any]


def _template_of(url: str) -> str:
    return parse_qs(urlparse(url).query)["template"][0]


def fixture_render(calls: list[str] | None = None, pdf: bytes | None = None) -> Render:
    """Serve the committed render of the requested template (or fixed bytes)."""

    async def render(url: str, *args: Any, **kwargs: Any) -> bytes:
        if calls is not None:
            calls.append(url)
        return pdf if pdf is not None else render_pdf(_template_of(url))

    return render


def output_size(body: dict[str, Any]) -> int:
    return len(json.dumps(body, separators=(",", ":")).encode())


@pytest.fixture(autouse=True)
def fast_busy_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(own_output, "RENDER_RETRY_BACKOFF_SECONDS", (0.01, 0.01))
    monkeypatch.setattr(ats, "PARSE_CHECK_BUSY_MAX_WAIT_SECONDS", 0.05)


@pytest.fixture
async def rendered_resume_id(isolated_db: Database) -> str:
    """Master resume whose data is exactly what the committed renders show."""
    data = render_source()
    resume = await isolated_db.create_resume(
        content=json.dumps(data),
        content_type="json",
        is_master=True,
        processed_data=data,
        processing_status="ready",
    )
    return resume["resume_id"]


def identity_tailoring() -> dict[str, Any]:
    """Tailored payload equal to the rendered source, so the renders match it."""
    return ResumeData.model_validate(copy.deepcopy(render_source())).model_dump()


async def add_job(client: Any) -> str:
    jobs = await client.call_tool("add_jobs", {"descriptions": [JOB_DESCRIPTION]})
    assert jobs.is_error is False, jobs.content
    return payload(jobs)["job_ids"][0]


class TestParseCheckFile:
    async def test_file_by_base64_returns_summary(self, isolated_db: Database) -> None:
        content = (FIXTURES / "clean_single_column.pdf").read_bytes()
        async with mcp_session() as (client, _):
            result = await client.call_tool(
                "ats_parse_check_file",
                {"filename": "cv.pdf", "content_base64": base64.b64encode(content).decode()},
            )
        assert result.is_error is False, result.content
        body = payload(result)
        assert body["status"] == "succeeded"
        assert body["file_format"] == "pdf"
        assert body["extractability"] == "full"
        assert body["passes"] is True
        assert isinstance(body["parseability_score"], int)
        assert all(check["message"] for check in body["failing_checks"])
        assert "report" not in body
        assert output_size(body) < SUMMARY_LIMIT_BYTES
        # Nothing is stored.
        assert (await isolated_db.get_stats())["total_resumes"] == 0

    async def test_local_path_on_stdio_and_detail(self, isolated_db: Database) -> None:
        async with mcp_session() as (client, _):
            result = await client.call_tool(
                "ats_parse_check_file",
                {"path": str(FIXTURES / "two_column.pdf"), "content_language": "en", "detail": True},
            )
        body = payload(result)
        assert body["content_language"] == "en"
        report = body["report"]
        assert report["schema_version"] == "2.0"
        assert {check["id"] for check in report["checks"]} >= {"multi_column", "text_layer"}
        failing = {check["id"]: check for check in body["failing_checks"]}
        assert failing["multi_column"]["message"].startswith("Multi-column layout detected.")
        assert "expected_by_template" not in failing["multi_column"]

    async def test_unreadable_file_fails_with_fatal_check(self, isolated_db: Database) -> None:
        content = (FIXTURES / "image_only.pdf").read_bytes()
        async with mcp_session() as (client, _):
            body = payload(
                await client.call_tool(
                    "ats_parse_check_file",
                    {"filename": "scan.pdf", "content_base64": base64.b64encode(content).decode()},
                )
            )
        assert body["passes"] is False
        failing = {check["id"]: check for check in body["failing_checks"]}
        assert failing["text_layer"]["severity"] == "fatal"
        assert "No selectable text" in failing["text_layer"]["message"]
        assert any("text_layer" in reason for reason in body["reasons"])

    async def test_oversized_file_rejected_before_bridge(self, isolated_db: Database) -> None:
        recorder = RecordingApp(app)
        oversized = base64.b64encode(b"%PDF" + b"0" * MAX_UPLOAD_BYTES).decode()
        async with mcp_session(asgi_app=recorder) as (client, _):
            result = await client.call_tool(
                "ats_parse_check_file", {"filename": "big.pdf", "content_base64": oversized}
            )
        assert "upload limit is 4 MB" in error_text(result)
        assert recorder.paths == []

    async def test_bad_suffix_rejected(self, isolated_db: Database, tmp_path: Path) -> None:
        notes = tmp_path / "resume.txt"
        notes.write_text("plain text")
        async with mcp_session() as (client, _):
            by_path = await client.call_tool("ats_parse_check_file", {"path": str(notes)})
            by_content = await client.call_tool(
                "ats_parse_check_file",
                {"filename": "resume.txt", "content_base64": base64.b64encode(b"x").decode()},
            )
        assert "Unsupported file type '.txt'" in error_text(by_path)
        assert "Unsupported file type '.txt'" in error_text(by_content)

    async def test_path_refused_over_http(self, isolated_db: Database) -> None:
        async with mcp_session(transport="http") as (client, _):
            result = await client.call_tool(
                "ats_parse_check_file", {"path": str(FIXTURES / "two_column.pdf")}
            )
        assert "only available on the stdio transport" in error_text(result)

    async def test_exactly_one_source_required(self, isolated_db: Database) -> None:
        async with mcp_session() as (client, _):
            neither = await client.call_tool("ats_parse_check_file", {})
            no_name = await client.call_tool("ats_parse_check_file", {"content_base64": "eA=="})
        assert "exactly one of path or content_base64" in error_text(neither)
        assert "filename is required" in error_text(no_name)


class TestParseCheckResume:
    async def test_single_template_summary(self, rendered_resume_id: str) -> None:
        calls: list[str] = []
        async with mcp_session() as (client, _):
            with patch("app.routers.resumes.render_resume_pdf", fixture_render(calls)):
                result = await client.call_tool(
                    "ats_parse_check_resume", {"resume_id": rendered_resume_id}
                )
        assert result.is_error is False, result.content
        body = payload(result)
        assert body["resume_id"] == rendered_resume_id
        assert body["render_locale"] == "en"
        [summary] = body["results"]
        assert summary["template"] == "swiss-single"
        assert summary["status"] == "ok"
        assert summary["passes"] is True
        assert summary["content_recall"] >= 0.95
        assert summary["order_fidelity"] >= 0.95
        assert "report" not in body
        assert output_size(body) < SUMMARY_LIMIT_BYTES
        assert [_template_of(url) for url in calls] == ["swiss-single"]

    async def test_render_locale_and_settings_reach_the_pdf_route(
        self, rendered_resume_id: str
    ) -> None:
        calls: list[str] = []
        async with mcp_session() as (client, _):
            with patch("app.routers.resumes.render_resume_pdf", fixture_render(calls)):
                body = payload(
                    await client.call_tool(
                        "ats_parse_check_resume",
                        {
                            "resume_id": rendered_resume_id,
                            "settings": {"template": "latex", "pageSize": "LETTER"},
                            "render_locale": "es",
                            "detail": True,
                        },
                    )
                )
        query = parse_qs(urlparse(calls[0]).query)
        assert query["template"] == ["latex"]
        assert query["pageSize"] == ["LETTER"]
        assert query["lang"] == ["es"]
        assert body["render_locale"] == "es"
        report = body["report"]
        assert report["settings"]["lang"] == "es"
        assert report["results"][0]["report"]["checks"]

    async def test_all_templates(self, rendered_resume_id: str) -> None:
        async with mcp_session() as (client, _):
            with patch("app.routers.resumes.render_resume_pdf", fixture_render()):
                body = payload(
                    await client.call_tool(
                        "ats_parse_check_resume",
                        {"resume_id": rendered_resume_id, "all_templates": True},
                    )
                )
        results = body["results"]
        assert [result["template"] for result in results] == list(TEMPLATE_IDS)
        for result in results:
            assert result["status"] == "ok"
            failing = {check["id"]: check for check in result["failing_checks"]}
            if result["template"] in TWO_COLUMN_TEMPLATES:
                assert result["two_column_by_design"] is True
                assert failing["multi_column"]["expected_by_template"] is True
                assert failing["multi_column"]["severity"] == "medium"
            else:
                assert "two_column_by_design" not in result
                assert "multi_column" not in failing

    async def test_busy_check_maps_429_to_retry_hint(
        self, rendered_resume_id: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(own_output, "_active_checks", own_output.MAX_CONCURRENT_CHECKS)
        async with mcp_session() as (client, _):
            result = await client.call_tool(
                "ats_parse_check_resume", {"resume_id": rendered_resume_id}
            )
        text = error_text(result)
        assert "Another parse check of rendered output is running" in text
        assert f"Retry in {own_output.BUSY_RETRY_AFTER_SECONDS} seconds." in text

    @pytest.mark.parametrize("bad_id", ["../applications", "x?y=1", "a/b", ""])
    async def test_invalid_id_never_reaches_the_app(
        self, isolated_db: Database, bad_id: str
    ) -> None:
        recorder = RecordingApp(app)
        async with mcp_session(asgi_app=recorder) as (client, _):
            by_resume = await client.call_tool("ats_parse_check_resume", {"resume_id": bad_id})
            by_tailor = await client.call_tool(
                "tailor_and_verify", {"resume_id": bad_id, "job_id": "job"}
            )
            by_job = await client.call_tool(
                "tailor_and_verify", {"resume_id": "resume", "job_id": bad_id}
            )
        assert "Invalid resume_id" in error_text(by_resume)
        assert "Invalid resume_id" in error_text(by_tailor)
        assert "Invalid job_id" in error_text(by_job)
        assert recorder.paths == []

    async def test_unknown_resume_is_not_found(self, isolated_db: Database) -> None:
        async with mcp_session() as (client, _):
            result = await client.call_tool("ats_parse_check_resume", {"resume_id": "missing"})
        assert "Resume not found" in error_text(result)


async def tailored_cards(db: Database, tailored_id: str) -> list[dict[str, Any]]:
    return [card for card in await db.list_applications() if card["resume_id"] == tailored_id]


class TestTailorAndVerify:
    async def test_happy_path_passes(self, isolated_db: Database, rendered_resume_id: str) -> None:
        async with mcp_session() as (client, _):
            job_id = await add_job(client)
            with (
                mocked_tailoring(identity_tailoring()),
                patch("app.routers.resumes.render_resume_pdf", fixture_render()),
            ):
                result = await client.call_tool(
                    "tailor_and_verify", {"resume_id": rendered_resume_id, "job_id": job_id}
                )
        assert result.is_error is False, result.content
        body = payload(result)
        assert body["status"] == "succeeded"
        assert body["passes"] is True
        assert body["template"] == "swiss-single"
        assert body["source_resume_id"] == rendered_resume_id
        assert body["job_id"] == job_id
        assert body["min_content_recall"] == 0.95
        assert body["content_recall"] >= 0.95
        assert body["order_fidelity"] >= 0.95
        assert isinstance(body["keyword_score"], (int, float))
        assert isinstance(body["parseability_score"], int)
        assert "content_score" in body
        assert isinstance(body["failing_checks"], list)
        assert "reasons" not in body and "report" not in body
        assert output_size(body) < SUMMARY_LIMIT_BYTES

        tailored_id = body["tailored_resume_id"]
        stored = await isolated_db.get_resume(tailored_id)
        assert stored["parent_id"] == rendered_resume_id
        cards = await tailored_cards(isolated_db, tailored_id)
        assert [card["application_id"] for card in cards] == [body["application_id"]]

    async def test_failing_parse_keeps_the_tailored_resume(
        self, isolated_db: Database, rendered_resume_id: str
    ) -> None:
        image_only = (FIXTURES / "image_only.pdf").read_bytes()
        async with mcp_session() as (client, _):
            job_id = await add_job(client)
            with (
                mocked_tailoring(identity_tailoring()),
                patch("app.routers.resumes.render_resume_pdf", fixture_render(pdf=image_only)),
            ):
                body = payload(
                    await client.call_tool(
                        "tailor_and_verify", {"resume_id": rendered_resume_id, "job_id": job_id}
                    )
                )
        assert body["status"] == "succeeded"
        assert body["passes"] is False
        failing = {check["id"]: check for check in body["failing_checks"]}
        assert failing["text_layer"]["severity"] == "fatal"
        assert body["content_recall"] < 0.95
        assert len(body["reasons"]) == 2
        # No rollback: the tailored resume and its tracker card persist.
        stored = await isolated_db.get_resume(body["tailored_resume_id"])
        assert stored is not None and stored["parent_id"] == rendered_resume_id
        assert len(await tailored_cards(isolated_db, body["tailored_resume_id"])) == 1

    async def test_two_column_template_counts_columns_as_medium(
        self, isolated_db: Database, rendered_resume_id: str
    ) -> None:
        """Column checks never fail a two-column template; recall still decides.

        The committed swiss-two-column render recovers 0.947 of its own source
        (two-column extraction misses a few lines), so the verdict is checked
        at 0.9 and, at the 0.95 default, fails on recall alone.
        """
        calls: list[str] = []
        async with mcp_session() as (client, _):
            job_id = await add_job(client)
            call = {"resume_id": rendered_resume_id, "job_id": job_id, "template": "swiss-two-column"}
            with (
                mocked_tailoring(identity_tailoring()),
                patch("app.routers.resumes.render_resume_pdf", fixture_render(calls)),
            ):
                relaxed = payload(
                    await client.call_tool("tailor_and_verify", {**call, "min_content_recall": 0.9})
                )
                default = payload(await client.call_tool("tailor_and_verify", call))
        assert {_template_of(url) for url in calls} == {"swiss-two-column"}
        assert relaxed["template"] == "swiss-two-column"
        assert relaxed["passes"] is True, relaxed.get("reasons")
        failing = {check["id"]: check for check in relaxed["failing_checks"]}
        assert failing["multi_column"]["expected_by_template"] is True
        assert failing["multi_column"]["severity"] == "medium"
        assert "Expected for the selected two-column template." in failing["multi_column"]["message"]
        assert default["passes"] is False
        assert default["reasons"] == [f"content_recall {default['content_recall']:g} is below 0.95"]

    async def test_detail_returns_full_reports(
        self, isolated_db: Database, rendered_resume_id: str
    ) -> None:
        async with mcp_session() as (client, _):
            job_id = await add_job(client)
            with (
                mocked_tailoring(identity_tailoring()),
                patch("app.routers.resumes.render_resume_pdf", fixture_render()),
            ):
                body = payload(
                    await client.call_tool(
                        "tailor_and_verify",
                        {"resume_id": rendered_resume_id, "job_id": job_id, "detail": True},
                    )
                )
        report = body["report"]
        assert report["resume_id"] == body["tailored_resume_id"]
        [template_result] = report["results"]
        assert template_result["report"]["roundtrip"]["fields"]
        assert body["keyword_score_detail"]["overall_score"] == body["keyword_score"]

    async def test_unrendered_content_lowers_recall_below_threshold(
        self, isolated_db: Database, rendered_resume_id: str
    ) -> None:
        """Content the PDF does not show counts against content_recall."""
        improved = identity_tailoring()
        improved["summary"] = (
            "Platform engineer who introduced contract testing across twelve partner "
            "integrations and cut release rollbacks by half."
        )
        async with mcp_session() as (client, _):
            job_id = await add_job(client)
            call = {"resume_id": rendered_resume_id, "job_id": job_id}
            with (
                mocked_tailoring(improved),
                patch("app.routers.resumes.render_resume_pdf", fixture_render()),
            ):
                strict = payload(
                    await client.call_tool("tailor_and_verify", {**call, "min_content_recall": 0.99})
                )
        assert 0.95 <= strict["content_recall"] < 0.99
        assert strict["passes"] is False
        assert strict["reasons"] == [f"content_recall {strict['content_recall']:g} is below 0.99"]
        assert strict["failing_checks"] == [] or all(
            check["severity"] not in ("fatal", "high") for check in strict["failing_checks"]
        )

    async def test_slow_run_returns_task_then_succeeds(
        self, isolated_db: Database, rendered_resume_id: str
    ) -> None:
        async def slow_render(url: str, *args: Any, **kwargs: Any) -> bytes:
            await asyncio.sleep(0.5)
            return render_pdf(_template_of(url))

        async with mcp_session() as (client, _):
            job_id = await add_job(client)
            with (
                mocked_tailoring(identity_tailoring()),
                patch("app.routers.resumes.render_resume_pdf", slow_render),
            ):
                started = payload(
                    await client.call_tool(
                        "tailor_and_verify",
                        {"resume_id": rendered_resume_id, "job_id": job_id, "wait_seconds": 0.1},
                    )
                )
                assert started["status"] == "running"
                assert "passes" not in started
                finished = await poll_task(client, started["task_id"], timeout=30)
        assert finished["status"] == "succeeded"
        assert finished["result"]["passes"] is True
        assert await isolated_db.get_resume(finished["result"]["tailored_resume_id"]) is not None

    async def test_retry_while_running_joins_the_same_task(
        self, isolated_db: Database, rendered_resume_id: str
    ) -> None:
        gate = asyncio.Event()
        renders: list[str] = []

        async def gated_render(url: str, *args: Any, **kwargs: Any) -> bytes:
            renders.append(url)
            await gate.wait()
            return render_pdf(_template_of(url))

        arguments = {"template": "latex"}
        async with mcp_session() as (client, _):
            job_id = await add_job(client)
            call = {"resume_id": rendered_resume_id, "job_id": job_id, **arguments}
            with (
                mocked_tailoring(identity_tailoring()),
                patch("app.routers.resumes.render_resume_pdf", gated_render),
            ):
                first = payload(
                    await client.call_tool("tailor_and_verify", {**call, "wait_seconds": 0})
                )
                retry = payload(
                    await client.call_tool("tailor_and_verify", {**call, "wait_seconds": 0})
                )
                other = payload(
                    await client.call_tool(
                        "tailor_and_verify",
                        {**call, "template": "clean", "wait_seconds": 0},
                    )
                )
                gate.set()
                finished = await poll_task(client, first["task_id"], timeout=30)
                await poll_task(client, other["task_id"], timeout=30)
                after = payload(await client.call_tool("tailor_and_verify", call))

        assert first["status"] == retry["status"] == "running"
        assert retry["task_id"] == first["task_id"]
        # Different settings are a different request.
        assert other["task_id"] != first["task_id"]
        assert finished["status"] == "succeeded"
        tailored_id = finished["result"]["tailored_resume_id"]
        # A repeat after completion returns the same result without new work.
        assert after["task_id"] == first["task_id"]
        assert after["tailored_resume_id"] == tailored_id
        latex_renders = [url for url in renders if _template_of(url) == "latex"]
        assert len(latex_renders) == 1
        assert len(await tailored_cards(isolated_db, tailored_id)) == 1

    async def test_busy_parse_check_is_retried(
        self,
        isolated_db: Database,
        rendered_resume_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        original = ats._post_own_output_check
        attempts: list[str] = []

        async def busy_once(runtime: Any, resume_id: str, body: dict[str, Any]) -> Any:
            attempts.append(resume_id)
            if len(attempts) == 1:
                monkeypatch.setattr(own_output, "_active_checks", own_output.MAX_CONCURRENT_CHECKS)
            else:
                monkeypatch.setattr(own_output, "_active_checks", 0)
            return await original(runtime, resume_id, body)

        monkeypatch.setattr(ats, "_post_own_output_check", busy_once)
        async with mcp_session() as (client, _):
            job_id = await add_job(client)
            with (
                mocked_tailoring(identity_tailoring()),
                patch("app.routers.resumes.render_resume_pdf", fixture_render()),
            ):
                body = payload(
                    await client.call_tool(
                        "tailor_and_verify", {"resume_id": rendered_resume_id, "job_id": job_id}
                    )
                )
        assert len(attempts) == 2
        assert body["passes"] is True

    async def test_parse_check_failure_names_the_saved_resume(
        self,
        isolated_db: Database,
        rendered_resume_id: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(own_output, "_active_checks", own_output.MAX_CONCURRENT_CHECKS)
        async with mcp_session() as (client, _):
            job_id = await add_job(client)
            with mocked_tailoring(identity_tailoring()):
                result = await client.call_tool(
                    "tailor_and_verify", {"resume_id": rendered_resume_id, "job_id": job_id}
                )
        text = error_text(result)
        assert "The tailored resume was saved" in text
        assert "Retry in" in text
        tailored = [
            resume
            for resume in await isolated_db.list_resumes()
            if resume.get("parent_id") == rendered_resume_id
        ]
        assert len(tailored) == 1
        assert f"tailored_resume_id={tailored[0]['resume_id']}" in text
