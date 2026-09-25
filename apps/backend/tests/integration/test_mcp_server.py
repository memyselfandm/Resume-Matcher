"""MCP server integration tests through the SDK's in-process client.

Each test builds a fresh server over the real FastAPI app (ASGI bridge, real
routers, the per-test isolated SQLite database). LLM-touching services are
replaced at the router boundary exactly as in ``test_pipeline_e2e.py``;
provider traffic is asserted absent with respx where it matters.

Regenerate the tool-surface snapshot after an intentional change with::

    uv run python -m tests.integration.test_mcp_server
"""

import asyncio
import base64
import copy
import json
import logging
import textwrap
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack, asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp_types import CallToolResult

from app.database import Database
from app.instance_id import get_db_instance_id
from app.main import app
from app.mcp.bridge import MAX_UPLOAD_BYTES, AppBridge
from app.mcp.runtime import MCPRuntime
from app.mcp.server import build_mcp_server
from app.schemas.models import InterviewPrepData, InterviewPrepQuestion, ResumeData

SNAPSHOT_PATH = Path(__file__).parent / "snapshots" / "mcp_tools.json"
MODERN = "2026-07-28"
FORBIDDEN_TOOL_FRAGMENTS = ("delete", "reset", "api_key", "config")


class RecordingApp:
    """ASGI wrapper recording every HTTP path dispatched to the real app."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.paths: list[str] = []

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            self.paths.append(scope["path"])
        await self.inner(scope, receive, send)


@asynccontextmanager
async def mcp_session(
    transport: str = "stdio", mode: str = MODERN, asgi_app: Any = app
) -> AsyncIterator[tuple[Client, MCPRuntime]]:
    """Connect an in-process SDK client to a freshly built server."""
    runtime = MCPRuntime(bridge=AppBridge(asgi_app), transport=transport)  # type: ignore[arg-type]
    server = build_mcp_server(runtime)
    try:
        async with Client(server, mode=mode, cache=None) as client:
            yield client, runtime
    finally:
        await runtime.aclose()


def payload(result: CallToolResult) -> dict[str, Any]:
    """Decode a tool result's JSON body."""
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def error_text(result: CallToolResult) -> str:
    """Return the text of an error result, asserting it is one."""
    assert result.is_error is True
    return " ".join(getattr(block, "text", "") for block in result.content)


def assert_no_internals(text: str) -> None:
    """Client-visible errors must not leak tracebacks or source paths."""
    assert "Traceback" not in text
    assert 'File "' not in text
    assert ".py" not in text


async def tool_surface() -> list[dict[str, Any]]:
    """Names and input schemas of every tool, in registration order."""
    runtime = MCPRuntime.create(app, transport="stdio")
    try:
        tools = await build_mcp_server(runtime).list_tools()
    finally:
        await runtime.aclose()
    return [{"name": tool.name, "inputSchema": tool.input_schema} for tool in tools]


@contextmanager
def mocked_upload_parsing(sample_resume: dict[str, Any]) -> Iterator[AsyncMock]:
    """Replace document conversion and LLM parsing inside the upload route."""
    with (
        patch(
            "app.routers.resumes.parse_document",
            new_callable=AsyncMock,
            return_value="# Jane Doe\nSenior Backend Engineer\njane@example.com\n",
        ) as parse_document,
        patch(
            "app.routers.resumes.parse_resume_to_json",
            new_callable=AsyncMock,
            return_value=copy.deepcopy(sample_resume),
        ),
    ):
        yield parse_document


def tailored_resume(sample_resume: dict[str, Any]) -> dict[str, Any]:
    """Canonical tailored payload: same personalInfo, rewritten summary."""
    improved = ResumeData.model_validate(copy.deepcopy(sample_resume)).model_dump()
    improved["summary"] = (
        "Senior backend engineer with 6 years building scalable Python and "
        "FastAPI services on AWS and Docker."
    )
    return improved


@contextmanager
def mocked_tailoring(improved: dict[str, Any]) -> Iterator[None]:
    """Replace every LLM-touching service used by preview and confirm."""
    with ExitStack() as stack:
        for target, kwargs in (
            (
                "app.routers.resumes.extract_job_keywords",
                {"new_callable": AsyncMock, "return_value": {"keywords": ["Python", "FastAPI"], "required_skills": []}},
            ),
            (
                "app.routers.resumes.generate_skill_target_plan",
                {"new_callable": AsyncMock, "return_value": {"accepted": [], "rejected": []}},
            ),
            ("app.routers.resumes.verify_skill_target_plan", {"return_value": {"accepted": [], "rejected": []}}),
            (
                "app.routers.resumes.generate_resume_diffs",
                {"new_callable": AsyncMock, "return_value": SimpleNamespace(changes=[])},
            ),
            ("app.routers.resumes.apply_diffs", {"return_value": (copy.deepcopy(improved), [], [])}),
            ("app.routers.resumes.verify_diff_result", {"return_value": []}),
            (
                "app.routers.resumes.refine_resume",
                {"new_callable": AsyncMock, "side_effect": RuntimeError("refinement disabled for test")},
            ),
            (
                "app.routers.resumes.generate_resume_title",
                {"new_callable": AsyncMock, "return_value": "Senior Backend Engineer - TechCorp"},
            ),
        ):
            stack.enter_context(patch(target, **kwargs))
        yield


async def upload_and_add_job(
    client: Client, sample_resume: dict[str, Any], job_description: str
) -> tuple[str, str]:
    """Upload the sample resume and one job through MCP tools."""
    with mocked_upload_parsing(sample_resume):
        uploaded = await client.call_tool(
            "upload_resume",
            {
                "filename": "resume.pdf",
                "content_base64": base64.b64encode(b"%PDF-1.4 fake").decode(),
            },
        )
    assert uploaded.is_error is False, uploaded.content
    upload_body = payload(uploaded)
    assert upload_body["processing_status"] == "ready"
    assert upload_body["is_master"] is True

    jobs = await client.call_tool("add_jobs", {"descriptions": [job_description]})
    assert jobs.is_error is False, jobs.content
    return upload_body["resume_id"], payload(jobs)["job_ids"][0]


async def poll_task(client: Client, task_id: str, timeout: float = 10.0) -> dict[str, Any]:
    """Poll get_task until the task leaves the running state."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        status = payload(await client.call_tool("get_task", {"task_id": task_id}))
        if status["status"] != "running":
            return status
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"task {task_id} still running after {timeout}s")
        await asyncio.sleep(0.1)


class TestSdkSurface:
    def test_pinned_sdk_imports(self) -> None:
        """The APIs this package (and the HTTP transport) rely on exist at the pin."""
        from importlib.metadata import version

        from mcp.server.caching import CacheHint
        from mcp.server.mcpserver import MCPServer
        from mcp.server.streamable_http_manager import StreamableHTTPASGIApp

        assert version("mcp") == "2.2.0"
        assert MCPServer.__name__ == "MCPServer"
        assert callable(StreamableHTTPASGIApp)
        assert CacheHint(ttl_ms=1).scope == "private"

    def test_console_script_is_declared(self) -> None:
        from importlib.metadata import entry_points

        scripts = {ep.name: ep.value for ep in entry_points(group="console_scripts")}
        assert scripts.get("resume-matcher-mcp") == "app.mcp.__main__:main"

    async def test_tool_list_matches_snapshot(self) -> None:
        expected = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        assert await tool_surface() == expected

    async def test_no_destructive_or_config_tools(self) -> None:
        names = [tool["name"] for tool in await tool_surface()]
        assert len(names) == len(set(names))
        for name in names:
            for fragment in FORBIDDEN_TOOL_FRAGMENTS:
                assert fragment not in name, name

    async def test_tools_list_is_stable_and_cacheable(self, isolated_db: Database) -> None:
        async with mcp_session() as (client, _):
            assert client.protocol_version == MODERN
            first = await client.list_tools()
            second = await client.list_tools()
        assert [tool.name for tool in first.tools] == [tool.name for tool in second.tools]
        assert first.ttl_ms == 3_600_000
        assert first.cache_scope == "private"
        assert first.result_type == "complete"

    async def test_legacy_handshake_client_lists_same_tools(self, isolated_db: Database) -> None:
        async with mcp_session(mode="legacy") as (client, _):
            assert client.protocol_version == "2025-11-25"
            tools = await client.list_tools()
        assert [tool.name for tool in tools.tools] == [tool["name"] for tool in await tool_surface()]


class TestResources:
    async def test_resume_resource_is_markdown_and_uncached(
        self, isolated_db: Database, sample_resume: dict[str, Any]
    ) -> None:
        async with mcp_session() as (client, _):
            resume_id, job_id = await upload_and_add_job(
                client, sample_resume, "Senior Python role at TechCorp."
            )
            resume = await client.read_resource(f"resume://{resume_id}")
            job = await client.read_resource(f"job://{job_id}")
        assert resume.ttl_ms == 0
        assert resume.contents[0].mime_type == "text/markdown"
        assert resume.contents[0].text.startswith("# Jane Doe")
        assert job.ttl_ms == 0
        assert job.contents[0].text == "Senior Python role at TechCorp."

    @pytest.mark.parametrize("uri", ["resume://does-not-exist", "job://does-not-exist"])
    async def test_unknown_resource_is_invalid_params(self, isolated_db: Database, uri: str) -> None:
        async with mcp_session() as (client, _):
            with pytest.raises(MCPError) as caught:
                await client.read_resource(uri)
        assert caught.value.error.code == -32602


class TestTailoringFlow:
    async def test_upload_jobs_preview_confirm_creates_child_and_one_card(
        self, isolated_db: Database, sample_resume: dict[str, Any]
    ) -> None:
        improved = tailored_resume(sample_resume)
        async with mcp_session() as (client, runtime):
            resume_id, job_id = await upload_and_add_job(
                client, sample_resume, "Senior Backend Engineer: Python, FastAPI, Docker, AWS."
            )
            with mocked_tailoring(improved):
                preview = await client.call_tool(
                    "tailor_resume_preview", {"resume_id": resume_id, "job_id": job_id}
                )
                assert preview.is_error is False, preview.content
                preview_body = payload(preview)
                assert preview_body["status"] == "succeeded"
                assert preview_body["task_id"]
                preview_id = preview_body["preview_id"]
                assert runtime.previews.get(preview_id) is not None
                # Preview alone persists nothing new.
                assert (await isolated_db.get_stats())["total_resumes"] == 1

                confirmed = await client.call_tool("tailor_resume_confirm", {"preview_id": preview_id})
            assert confirmed.is_error is False, confirmed.content
            confirm_body = payload(confirmed)
            assert confirm_body["status"] == "succeeded"

            tailored_id = confirm_body["tailored_resume_id"]
            fetched = payload(
                await client.call_tool("get_resume", {"resume_id": tailored_id, "format": "json"})
            )

        assert tailored_id != resume_id
        assert fetched["parent_id"] == resume_id
        assert fetched["resume_data"]["summary"] == improved["summary"]

        stored = await isolated_db.get_resume(tailored_id)
        assert stored["parent_id"] == resume_id
        assert stored["is_master"] is False

        cards = [
            card
            for card in await isolated_db.list_applications()
            if card["job_id"] == job_id and card["resume_id"] == tailored_id
        ]
        assert len(cards) == 1
        assert confirm_body["application_id"] == cards[0]["application_id"]
        # The preview handle stays valid so a retry can replay the result.
        assert runtime.previews.get(preview_id) is not None

    async def test_confirm_retry_replays_same_result(
        self, isolated_db: Database, sample_resume: dict[str, Any]
    ) -> None:
        improved = tailored_resume(sample_resume)
        async with mcp_session() as (client, _):
            resume_id, job_id = await upload_and_add_job(
                client, sample_resume, "Senior Backend Engineer: Python, FastAPI."
            )
            with mocked_tailoring(improved):
                preview_id = payload(
                    await client.call_tool(
                        "tailor_resume_preview", {"resume_id": resume_id, "job_id": job_id}
                    )
                )["preview_id"]
                first = payload(await client.call_tool("tailor_resume_confirm", {"preview_id": preview_id}))
                second = payload(await client.call_tool("tailor_resume_confirm", {"preview_id": preview_id}))

        assert second["tailored_resume_id"] == first["tailored_resume_id"]
        assert second["application_id"] == first["application_id"]
        stats = await isolated_db.get_stats()
        assert stats["total_resumes"] == 2
        cards = [
            card
            for card in await isolated_db.list_applications()
            if card["resume_id"] == first["tailored_resume_id"]
        ]
        assert len(cards) == 1

    async def test_confirm_retry_while_running_joins_the_same_task(
        self, isolated_db: Database, sample_resume: dict[str, Any]
    ) -> None:
        improved = tailored_resume(sample_resume)

        async def slow_title(*args: Any, **kwargs: Any) -> str:
            await asyncio.sleep(1.0)
            return "Senior Backend Engineer - TechCorp"

        async with mcp_session() as (client, _):
            resume_id, job_id = await upload_and_add_job(
                client, sample_resume, "Senior Backend Engineer: Python, FastAPI."
            )
            with mocked_tailoring(improved):
                preview_id = payload(
                    await client.call_tool(
                        "tailor_resume_preview", {"resume_id": resume_id, "job_id": job_id}
                    )
                )["preview_id"]
                with patch("app.routers.resumes.generate_resume_title", slow_title):
                    first = payload(
                        await client.call_tool(
                            "tailor_resume_confirm", {"preview_id": preview_id, "wait_seconds": 0.2}
                        )
                    )
                    retry = await client.call_tool(
                        "tailor_resume_confirm", {"preview_id": preview_id, "wait_seconds": 0}
                    )
                    assert retry.is_error is False, retry.content
                    retry_body = payload(retry)
                    finished = await poll_task(client, first["task_id"])
                after = payload(
                    await client.call_tool("tailor_resume_confirm", {"preview_id": preview_id})
                )

        assert first["status"] == "running"
        assert retry_body["status"] == "running"
        assert retry_body["task_id"] == first["task_id"]
        assert finished["status"] == "succeeded"
        tailored_id = finished["result"]["tailored_resume_id"]
        assert after["tailored_resume_id"] == tailored_id
        assert after["application_id"] == finished["result"]["application_id"]
        assert (await isolated_db.get_stats())["total_resumes"] == 2
        cards = [c for c in await isolated_db.list_applications() if c["resume_id"] == tailored_id]
        assert len(cards) == 1

    async def test_preview_cache_miss_is_tool_error(self, isolated_db: Database) -> None:
        async with mcp_session() as (client, _):
            result = await client.call_tool("tailor_resume_confirm", {"preview_id": "unknown-preview"})
        text = error_text(result)
        assert "run tailor_resume_preview again" in text
        assert_no_internals(text)
        assert (await isolated_db.get_stats())["total_resumes"] == 0

    async def test_slow_preview_returns_task_then_succeeds(
        self, isolated_db: Database, sample_resume: dict[str, Any]
    ) -> None:
        import app.routers.resumes as resumes_router

        original_flow = resumes_router._improve_preview_flow

        async def slow_flow(**kwargs: Any) -> Any:
            await asyncio.sleep(1.5)
            return await original_flow(**kwargs)

        async with mcp_session() as (client, _):
            resume_id, job_id = await upload_and_add_job(
                client, sample_resume, "Senior Backend Engineer: Python, FastAPI."
            )
            with (
                mocked_tailoring(tailored_resume(sample_resume)),
                patch.object(resumes_router, "_improve_preview_flow", slow_flow),
            ):
                started = payload(
                    await client.call_tool(
                        "tailor_resume_preview",
                        {"resume_id": resume_id, "job_id": job_id, "wait_seconds": 1},
                    )
                )
                assert started["status"] == "running"
                assert started["task_id"]
                assert "preview_id" not in started

                finished = await poll_task(client, started["task_id"])
        assert finished["status"] == "succeeded"
        assert finished["result"]["preview_id"]
        assert finished["result"]["job_id"] == job_id


class TestErrorMapping:
    async def test_unknown_resume_returns_router_detail(self, isolated_db: Database) -> None:
        async with mcp_session() as (client, _):
            result = await client.call_tool("get_resume", {"resume_id": "does-not-exist"})
        text = error_text(result)
        assert "Resume not found" in text
        assert_no_internals(text)

    async def test_unhandled_router_exception_is_generic_and_logged(
        self,
        isolated_db: Database,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        async def explode(job_id: str) -> None:
            raise RuntimeError("sqlite at /private/data/secret.db is corrupt")

        monkeypatch.setattr(isolated_db, "get_job", explode)
        with caplog.at_level(logging.ERROR, logger="app.mcp.bridge"):
            async with mcp_session() as (client, _):
                result = await client.call_tool("get_job", {"job_id": "any"})
        text = error_text(result)
        assert "failed to complete the request" in text
        assert "secret" not in text
        assert_no_internals(text)
        logged = [r for r in caplog.records if r.name == "app.mcp.bridge" and r.exc_info]
        assert len(logged) == 1
        assert logged[0].getMessage() == "Unhandled exception in MCP bridge request GET /api/v1/jobs/any"
        assert "secret.db is corrupt" in str(logged[0].exc_info[1])


UNSAFE_IDS = ["../applications/bulk", "..%2F", "x?y=1", "x#y", "a/b", "", "x" * 129]
ID_TOOLS: list[tuple[str, dict[str, Any]]] = [
    ("get_resume", {}),
    ("update_resume", {"resume_data": {}}),
    ("set_resume_title", {"title": "x"}),
    ("generate_cover_letter", {}),
    ("generate_outreach", {}),
    ("generate_interview_prep", {}),
    ("export_resume_pdf", {}),
]


class TestPathInjection:
    @pytest.mark.parametrize("bad_id", UNSAFE_IDS)
    async def test_unsafe_ids_never_reach_the_app(self, isolated_db: Database, bad_id: str) -> None:
        recorder = RecordingApp(app)
        calls = [(name, {"resume_id": bad_id, **extra}) for name, extra in ID_TOOLS]
        calls += [
            ("get_job", {"job_id": bad_id}),
            ("update_application", {"application_id": bad_id, "notes": "x"}),
        ]
        async with mcp_session(asgi_app=recorder) as (client, _):
            for name, arguments in calls:
                result = await client.call_tool(name, arguments)
                text = error_text(result)
                assert "Invalid" in text, (name, text)
            for uri in (f"resume://{bad_id}", f"job://{bad_id}"):
                with pytest.raises(MCPError) as caught:
                    await client.read_resource(uri)
                assert caught.value.error.code == -32602
        assert recorder.paths == []

    async def test_valid_ids_still_dispatch(self, isolated_db: Database) -> None:
        recorder = RecordingApp(app)
        async with mcp_session(asgi_app=recorder) as (client, _):
            result = await client.call_tool("get_job", {"job_id": "3f1c2a9e-6b1d-4c3e-9a57-0e2f8b6d4c11"})
        assert "Job not found" in error_text(result)
        assert recorder.paths == ["/api/v1/jobs/3f1c2a9e-6b1d-4c3e-9a57-0e2f8b6d4c11"]


class TestUploadGuards:
    async def test_oversized_base64_rejected_before_bridge(
        self, isolated_db: Database, sample_resume: dict[str, Any]
    ) -> None:
        oversized = base64.b64encode(b"%PDF" + b"0" * MAX_UPLOAD_BYTES).decode()
        async with mcp_session() as (client, _):
            with mocked_upload_parsing(sample_resume) as parse_document:
                result = await client.call_tool(
                    "upload_resume", {"filename": "big.pdf", "content_base64": oversized}
                )
        assert "upload limit is 4 MB" in error_text(result)
        parse_document.assert_not_awaited()
        assert (await isolated_db.get_stats())["total_resumes"] == 0

    async def test_oversized_local_file_rejected(
        self, isolated_db: Database, sample_resume: dict[str, Any], tmp_path: Path
    ) -> None:
        big = tmp_path / "big.pdf"
        big.write_bytes(b"0" * (MAX_UPLOAD_BYTES + 1))
        async with mcp_session() as (client, _):
            with mocked_upload_parsing(sample_resume) as parse_document:
                result = await client.call_tool("upload_resume", {"path": str(big)})
        assert "upload limit is 4 MB" in error_text(result)
        parse_document.assert_not_awaited()

    async def test_wrong_suffix_rejected(self, isolated_db: Database, tmp_path: Path) -> None:
        notes = tmp_path / "resume.txt"
        notes.write_text("plain text")
        async with mcp_session() as (client, _):
            by_path = await client.call_tool("upload_resume", {"path": str(notes)})
            by_content = await client.call_tool(
                "upload_resume",
                {"filename": "resume.txt", "content_base64": base64.b64encode(b"x").decode()},
            )
        assert "Unsupported file type '.txt'" in error_text(by_path)
        assert "Unsupported file type '.txt'" in error_text(by_content)

    async def test_local_path_upload_works_on_stdio(
        self, isolated_db: Database, sample_resume: dict[str, Any], tmp_path: Path
    ) -> None:
        resume_file = tmp_path / "resume.pdf"
        resume_file.write_bytes(b"%PDF-1.4 fake")
        async with mcp_session() as (client, _):
            with mocked_upload_parsing(sample_resume):
                result = await client.call_tool("upload_resume", {"path": str(resume_file)})
        body = payload(result)
        assert body["processing_status"] == "ready"
        assert await isolated_db.get_resume(body["resume_id"]) is not None

    async def test_line_wrapped_base64_is_accepted(
        self, isolated_db: Database, sample_resume: dict[str, Any]
    ) -> None:
        encoded = base64.b64encode(b"%PDF-1.4 " + b"x" * 300).decode()
        wrapped = "\n".join(textwrap.wrap(encoded, 76)) + "\n"
        assert "\n" in wrapped.strip()
        async with mcp_session() as (client, _):
            with mocked_upload_parsing(sample_resume) as parse_document:
                result = await client.call_tool(
                    "upload_resume", {"filename": "resume.pdf", "content_base64": wrapped}
                )
        assert payload(result)["processing_status"] == "ready"
        assert parse_document.await_args.args[0] == b"%PDF-1.4 " + b"x" * 300

    async def test_local_path_upload_refused_on_http(self, isolated_db: Database, tmp_path: Path) -> None:
        resume_file = tmp_path / "resume.pdf"
        resume_file.write_bytes(b"%PDF-1.4 fake")
        async with mcp_session(transport="http") as (client, _):
            result = await client.call_tool("upload_resume", {"path": str(resume_file)})
        assert "only available on the stdio transport" in error_text(result)


@contextmanager
def probe_mocks(health_body: dict[str, Any] | None) -> Iterator[respx.MockRouter]:
    """Mock the backend/frontend probes; any other HTTP request fails the test.

    litellm is forced onto httpx so a provider call would be intercepted too.
    """
    import litellm

    with (
        patch.object(litellm, "disable_aiohttp_transport", True),
        respx.mock(assert_all_called=False, assert_all_mocked=True) as router,
    ):
        router.post(host="api.openai.com", name="openai")
        if health_body is None:
            router.get("http://127.0.0.1:8000/api/v1/health", name="backend").mock(
                side_effect=httpx.ConnectError("refused")
            )
        else:
            router.get("http://127.0.0.1:8000/api/v1/health", name="backend").respond(
                200, json=health_body
            )
        router.get(host="localhost", port=3000, name="frontend").respond(200, text="<html></html>")
        yield router


async def seed_resume(db: Database) -> None:
    await db.create_resume(content="# Jane Doe", processing_status="ready", is_master=True)


async def real_health(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> dict[str, Any]:
    """Call the real /health route as a backend running on ``data_dir``."""
    from app.config import settings

    with monkeypatch.context() as patched:
        patched.setattr(settings, "data_dir", data_dir)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://backend"
        ) as backend:
            return (await backend.get("/api/v1/health")).json()


class TestGetStatus:
    async def test_configured_key_triggers_no_llm_calls(self, isolated_db: Database) -> None:
        from app.config import save_api_keys_to_config

        save_api_keys_to_config({"openai": "sk-test-configured"})
        health = {"status": "healthy", "db_instance_id": get_db_instance_id()}
        with (
            probe_mocks(health) as router,
            patch("app.llm.check_llm_health", new_callable=AsyncMock) as llm_health,
            patch("app.routers.health.check_llm_health", new_callable=AsyncMock) as route_health,
        ):
            async with mcp_session() as (client, _):
                status = payload(await client.call_tool("get_status", {}))
            assert not router["openai"].called
            # Only the two readiness probes went out.
            hosts = sorted(call.request.url.host for call in router.calls)
        assert hosts == ["127.0.0.1", "localhost"]
        assert status["llm_configured"] is True
        llm_health.assert_not_awaited()
        route_health.assert_not_awaited()
        assert status["frontend"]["reachable"] is True

    async def test_render_path_unknown_when_both_databases_empty(
        self, isolated_db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Neither this process's data dir nor the backend's holds a database.
        health = await real_health(monkeypatch, tmp_path / "empty-backend-data")
        assert health["db_instance_id"] is None
        with probe_mocks(health):
            async with mcp_session() as (client, _):
                status = payload(await client.call_tool("get_status", {}))
        assert status["render_path_ok"] == "unknown"
        assert status["pdf_export_ready"] is False

    async def test_render_path_false_for_different_data_dir(
        self, isolated_db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        other_dir = tmp_path / "other-data"
        other_db = Database(db_path=other_dir / "resume_matcher.db")
        await other_db.get_stats()  # establish the other backend's database
        await other_db.close()
        health = await real_health(monkeypatch, other_dir)
        assert health["db_instance_id"]
        await isolated_db.get_stats()  # establish this (still empty) database
        with probe_mocks(health):
            async with mcp_session() as (client, _):
                status = payload(await client.call_tool("get_status", {}))
        # Both ids exist and differ: false even though this database is empty.
        assert status["database"]["total_resumes"] == 0
        assert status["render_path_ok"] is False
        assert "different data directory" in status["render_path_detail"]

    async def test_render_path_false_when_only_one_side_has_a_database(
        self, isolated_db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await seed_resume(isolated_db)
        health = await real_health(monkeypatch, tmp_path / "empty-backend-data")
        with probe_mocks(health):
            async with mcp_session() as (client, _):
                status = payload(await client.call_tool("get_status", {}))
        assert status["render_path_ok"] is False

    async def test_render_path_true_for_same_data_dir(self, isolated_db: Database) -> None:
        await seed_resume(isolated_db)
        # The real /health route on this process's data directory.
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://backend"
        ) as backend:
            health = (await backend.get("/api/v1/health")).json()
        with probe_mocks(health):
            async with mcp_session() as (client, _):
                status = payload(await client.call_tool("get_status", {}))
        assert status["render_path_ok"] is True
        assert status["db_instance_id"] == health["db_instance_id"]
        assert status["pdf_export_ready"] is True

    async def test_render_path_unknown_for_backend_without_instance_id(
        self, isolated_db: Database
    ) -> None:
        await seed_resume(isolated_db)
        with probe_mocks({"status": "healthy"}):
            async with mcp_session() as (client, _):
                status = payload(await client.call_tool("get_status", {}))
        assert status["render_path_ok"] == "unknown"

    async def test_render_path_false_when_backend_unreachable(self, isolated_db: Database) -> None:
        await seed_resume(isolated_db)
        with probe_mocks(None):
            async with mcp_session() as (client, _):
                status = payload(await client.call_tool("get_status", {}))
        assert status["render_path_ok"] is False
        assert "No backend answered" in status["render_path_detail"]


class TestHealthInstanceId:
    async def test_health_reports_stable_data_dir_identity(self, isolated_db: Database) -> None:
        from app.config import settings

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://backend"
        ) as backend:
            before = (await backend.get("/api/v1/health")).json()
            await seed_resume(isolated_db)
            first = (await backend.get("/api/v1/health")).json()
            second = (await backend.get("/api/v1/health")).json()
        assert before == {"status": "healthy", "db_instance_id": None}
        assert first["status"] == "healthy"
        assert first["db_instance_id"] == second["db_instance_id"]
        assert (settings.data_dir / "instance_id").read_text() == first["db_instance_id"]


async def create_tailored_resume(
    client: Client, sample_resume: dict[str, Any]
) -> dict[str, Any]:
    """Run upload -> add_jobs -> preview -> confirm and return the confirm body."""
    resume_id, job_id = await upload_and_add_job(
        client, sample_resume, "Senior Backend Engineer: Python, FastAPI."
    )
    with mocked_tailoring(tailored_resume(sample_resume)):
        preview_id = payload(
            await client.call_tool("tailor_resume_preview", {"resume_id": resume_id, "job_id": job_id})
        )["preview_id"]
        return payload(await client.call_tool("tailor_resume_confirm", {"preview_id": preview_id}))


class TestDocumentTools:
    async def test_generators_save_content_for_tailored_resume(
        self, isolated_db: Database, sample_resume: dict[str, Any]
    ) -> None:
        prep = InterviewPrepData(
            role_fit_analysis=["Strong API background"],
            resume_questions=[InterviewPrepQuestion(question="Describe the migration.")],
            project_follow_ups=[],
            skill_gaps=[],
            talking_points=["Throughput gains"],
        )
        async with mcp_session() as (client, _):
            tailored_id = (await create_tailored_resume(client, sample_resume))["tailored_resume_id"]
            with (
                patch("app.routers.resumes.generate_cover_letter", new_callable=AsyncMock, return_value="Dear team"),
                patch("app.routers.resumes.generate_outreach_message", new_callable=AsyncMock, return_value="Hi there"),
                patch("app.routers.resumes.generate_interview_prep", new_callable=AsyncMock, return_value=prep),
            ):
                cover = payload(await client.call_tool("generate_cover_letter", {"resume_id": tailored_id}))
                outreach = payload(await client.call_tool("generate_outreach", {"resume_id": tailored_id}))
                interview = payload(await client.call_tool("generate_interview_prep", {"resume_id": tailored_id}))

        assert cover["status"] == "succeeded" and cover["cover_letter"] == "Dear team"
        assert outreach["outreach_message"] == "Hi there"
        assert interview["interview_prep"]["talking_points"] == ["Throughput gains"]
        stored = await isolated_db.get_resume(tailored_id)
        assert stored["cover_letter"] == "Dear team"
        assert stored["outreach_message"] == "Hi there"
        assert json.loads(stored["interview_prep"])["role_fit_analysis"] == ["Strong API background"]

    async def test_export_pdf_to_file_and_base64(
        self, isolated_db: Database, sample_resume: dict[str, Any], tmp_path: Path
    ) -> None:
        pdf = b"%PDF-1.7 rendered"
        out_file = tmp_path / "cv.pdf"
        async with mcp_session() as (client, _):
            with mocked_upload_parsing(sample_resume):
                resume_id = payload(
                    await client.call_tool(
                        "upload_resume",
                        {"filename": "r.pdf", "content_base64": base64.b64encode(b"%PDF-1.4").decode()},
                    )
                )["resume_id"]
            with patch("app.routers.resumes.render_resume_pdf", new_callable=AsyncMock, return_value=pdf) as render:
                to_file = payload(
                    await client.call_tool(
                        "export_resume_pdf",
                        {"resume_id": resume_id, "template": "modern", "out_path": str(out_file)},
                    )
                )
                inline = payload(await client.call_tool("export_resume_pdf", {"resume_id": resume_id}))

        assert to_file["path"] == str(out_file) and "content_base64" not in to_file
        assert out_file.read_bytes() == pdf
        assert base64.b64decode(inline["content_base64"]) == pdf
        first_url = render.await_args_list[0].args[0]
        assert f"/print/resumes/{resume_id}?template=modern" in first_url

    async def test_polled_pdf_payload_is_returned_once(
        self, isolated_db: Database, sample_resume: dict[str, Any]
    ) -> None:
        pdf = b"%PDF-1.7 rendered"
        async with mcp_session() as (client, _):
            with mocked_upload_parsing(sample_resume):
                resume_id = payload(
                    await client.call_tool(
                        "upload_resume",
                        {"filename": "r.pdf", "content_base64": base64.b64encode(b"%PDF-1.4").decode()},
                    )
                )["resume_id"]

            async def slow_render(*args: Any, **kwargs: Any) -> bytes:
                await asyncio.sleep(0.3)
                return pdf

            with patch("app.routers.resumes.render_resume_pdf", slow_render):
                started = payload(
                    await client.call_tool("export_resume_pdf", {"resume_id": resume_id, "wait_seconds": 0})
                )
                assert started["status"] == "running"
                first = await poll_task(client, started["task_id"])
            second = payload(await client.call_tool("get_task", {"task_id": started["task_id"]}))
        assert base64.b64decode(first["result"]["content_base64"]) == pdf
        assert second["status"] == "succeeded"
        assert "content_base64" not in second["result"]
        assert second["result"]["payload_released"] is True


class TestTrackerTools:
    async def test_create_list_and_update_application(
        self, isolated_db: Database, sample_resume: dict[str, Any]
    ) -> None:
        async with mcp_session() as (client, _):
            resume_id, _ = await upload_and_add_job(client, sample_resume, "Backend role.")
            created = payload(
                await client.call_tool(
                    "create_application",
                    {
                        "resume_id": resume_id,
                        "job_description": "Platform engineer at Initech.",
                        "company": "Initech",
                        "role": "Platform Engineer",
                        "status": "saved",
                    },
                )
            )
            application_id = created["application_id"]
            saved = payload(await client.call_tool("list_applications", {"status": "saved"}))
            updated = payload(
                await client.call_tool(
                    "update_application",
                    {"application_id": application_id, "status": "interview", "notes": "Phone screen"},
                )
            )
            board = payload(await client.call_tool("list_applications", {}))

        assert created["company"] == "Initech" and created["status"] == "saved"
        assert [card["application_id"] for card in saved["columns"]["saved"]] == [application_id]
        assert saved["total"] == 1
        assert updated["status"] == "interview" and updated["notes"] == "Phone screen"
        assert [card["application_id"] for card in board["columns"]["interview"]] == [application_id]
        stored = await isolated_db.get_application(application_id)
        assert stored["status"] == "interview"
        assert stored["notes"] == "Phone screen"


class TestTaskTools:
    async def test_unknown_task_is_tool_error(self, isolated_db: Database) -> None:
        async with mcp_session() as (client, _):
            result = await client.call_tool("get_task", {"task_id": "nope"})
        assert "Unknown or expired task_id" in error_text(result)

    async def test_cancel_task_stops_running_operation(self, isolated_db: Database) -> None:
        async with mcp_session() as (client, runtime):
            gate = asyncio.Event()

            async def blocked() -> dict[str, Any]:
                await gate.wait()
                return {}

            record = runtime.tasks.start("blocked", blocked)
            cancelled = payload(await client.call_tool("cancel_task", {"task_id": record.task_id}))
        assert cancelled["status"] == "cancelled"


def _write_snapshot() -> None:
    """Regenerate the committed tool-surface snapshot."""
    surface = asyncio.run(tool_surface())
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_PATH.write_text(json.dumps(surface, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    _write_snapshot()
