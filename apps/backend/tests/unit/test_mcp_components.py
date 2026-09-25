"""Unit tests for the MCP server's building blocks.

Covers the preview cache, task registry, bridge error translation, upload
guards, wait-time policy, Markdown rendering and the database instance id.
"""

import asyncio
import multiprocessing
import threading
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from app.instance_id import (
    INSTANCE_ID_FILENAME,
    database_established,
    get_db_instance_id,
)
from app.mcp.bridge import (
    GENERIC_SERVER_ERROR,
    MAX_UPLOAD_BYTES,
    UPLOAD_CONTENT_TYPES,
    AppBridge,
    InvalidIdentifierError,
    _error_message,
    check_upload_size,
    path_segment,
    upload_content_type,
)
from app.mcp.formatting import resume_markdown, resume_summary
from app.mcp.previews import CachedPreview, PreviewCache, preview_expiry
from app.mcp.runtime import (
    HTTP_DEFAULT_WAIT_SECONDS,
    STDIO_DEFAULT_WAIT_SECONDS,
    MCPRuntime,
)
from app.mcp.tasks import GENERIC_TASK_ERROR, TaskCapacityError, TaskRegistry


class FakeClock:
    """Manually advanced clock for deterministic expiry tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _preview(preview_id: str, expires_at: float) -> CachedPreview:
    return CachedPreview(
        preview_id=preview_id,
        resume_id="r1",
        job_id="j1",
        improved_data={"summary": preview_id},
        improvements=[],
        expires_at=expires_at,
    )


class TestPreviewCache:
    def test_get_returns_stored_preview(self) -> None:
        cache = PreviewCache(clock=FakeClock())
        cache.put(_preview("p1", expires_at=2000.0))
        cached = cache.get("p1")
        assert cached is not None
        assert cached.improved_data == {"summary": "p1"}

    def test_expired_preview_is_a_miss_and_is_dropped(self) -> None:
        clock = FakeClock()
        cache = PreviewCache(clock=clock)
        cache.put(_preview("p1", expires_at=1500.0))
        clock.now = 1500.0
        assert cache.get("p1") is None
        assert len(cache) == 0

    def test_lru_evicts_least_recently_used(self) -> None:
        cache = PreviewCache(max_entries=2, clock=FakeClock())
        cache.put(_preview("a", 9999.0))
        cache.put(_preview("b", 9999.0))
        assert cache.get("a") is not None  # "a" becomes most recent
        cache.put(_preview("c", 9999.0))
        assert cache.get("b") is None
        assert cache.get("a") is not None
        assert cache.get("c") is not None

    def test_preview_expiry_parses_iso_and_falls_back_to_ttl(self) -> None:
        assert preview_expiry("1970-01-01T00:16:40+00:00", 60, now=0.0) == 1000.0
        assert preview_expiry(None, 60, now=10.0) == 70.0
        assert preview_expiry("not-a-date", 60, now=10.0) == 70.0


class TestTaskRegistry:
    async def test_successful_task_reports_result(self) -> None:
        registry = TaskRegistry()

        async def work() -> dict[str, Any]:
            return {"value": 42}

        record = registry.start("work", work)
        settled = await registry.wait(record.task_id, 5)
        assert settled is not None
        assert settled.status == "succeeded"
        assert settled.to_dict() == {
            "task_id": record.task_id,
            "name": "work",
            "status": "succeeded",
            "result": {"value": 42},
        }

    async def test_presenter_shapes_serialized_result_only(self) -> None:
        registry = TaskRegistry()

        async def work() -> dict[str, Any]:
            return {"value": 42, "bulky": "x" * 10}

        def present(result: dict[str, Any]) -> dict[str, Any]:
            return {"value": result["value"]}

        record = registry.start("work", work, present)
        await registry.wait(record.task_id, 5)
        assert record.to_dict()["result"] == {"value": 42}
        assert record.result == {"value": 42, "bulky": "x" * 10}

    async def test_joined_calls_each_get_their_own_view(self) -> None:
        runtime = MCPRuntime(bridge=None, transport="stdio")  # type: ignore[arg-type]
        started: list[int] = []

        async def work() -> dict[str, Any]:
            started.append(1)
            return {"value": 42}

        first = await runtime.run_long_operation(
            "work", work, 5, idempotency_key="k", presenter=lambda r: {"shown": r["value"]}
        )
        second = await runtime.run_long_operation(
            "work", work, 5, idempotency_key="k", presenter=lambda r: {"doubled": r["value"] * 2}
        )
        assert started == [1]
        assert first["task_id"] == second["task_id"]
        assert first["shown"] == 42 and "value" not in first
        assert second["doubled"] == 84 and "shown" not in second
        await runtime.tasks.shutdown()

    async def test_wait_times_out_without_cancelling(self) -> None:
        registry = TaskRegistry()
        release = asyncio.Event()

        async def work() -> dict[str, Any]:
            await release.wait()
            return {"done": True}

        record = registry.start("slow", work)
        assert (await registry.wait(record.task_id, 0.05)).status == "running"
        release.set()
        assert (await registry.wait(record.task_id, 5)).status == "succeeded"

    async def test_tool_error_message_is_kept_other_errors_are_generic(self) -> None:
        registry = TaskRegistry()

        async def expected() -> dict[str, Any]:
            raise ToolError("Resume not found")

        async def crash() -> dict[str, Any]:
            raise RuntimeError("internal path /secret/db leaked")

        first = registry.start("expected", expected)
        second = registry.start("crash", crash)
        assert (await registry.wait(first.task_id, 5)).error == "Resume not found"
        crashed = await registry.wait(second.task_id, 5)
        assert crashed.status == "failed"
        assert crashed.error == GENERIC_TASK_ERROR
        assert "secret" not in str(crashed.to_dict())

    async def test_cancel_marks_task_cancelled(self) -> None:
        registry = TaskRegistry()

        async def forever() -> dict[str, Any]:
            await asyncio.sleep(3600)
            return {}

        record = registry.start("forever", forever)
        registry.cancel(record.task_id)
        settled = await registry.wait(record.task_id, 5)
        assert settled.status == "cancelled"

    async def test_capacity_rejects_new_work_instead_of_dropping_unread_results(self) -> None:
        registry = TaskRegistry(max_tasks=1)
        release = asyncio.Event()

        async def blocked() -> dict[str, Any]:
            await release.wait()
            return {"answer": 1}

        record = registry.start("blocked", blocked)
        with pytest.raises(TaskCapacityError):
            registry.start("second", blocked)
        release.set()
        await registry.wait(record.task_id, 5)
        # Finished but unread: the result is kept and new work is refused.
        with pytest.raises(TaskCapacityError, match="unread results"):
            registry.start("third", blocked)
        assert registry.get(record.task_id).result == {"answer": 1}
        # Once read, the slot is reusable.
        registry.get(record.task_id).mark_delivered()
        again = registry.start("fourth", blocked)
        assert registry.get(again.task_id) is not None
        assert registry.get(record.task_id) is None
        await registry.shutdown()

    async def test_large_payload_released_after_delivery(self) -> None:
        registry = TaskRegistry()

        async def export() -> dict[str, Any]:
            return {"bytes": 3, "content_base64": "UERG"}

        record = registry.start("export", export)
        await registry.wait(record.task_id, 5)
        first = record.to_dict()
        record.mark_delivered()
        assert first["result"]["content_base64"] == "UERG"
        assert record.to_dict()["result"] == {"bytes": 3, "payload_released": True}

    async def test_running_task_is_never_marked_delivered(self) -> None:
        registry = TaskRegistry()
        release = asyncio.Event()

        async def work() -> dict[str, Any]:
            await release.wait()
            return {}

        record = registry.start("work", work)
        record.mark_delivered()
        assert record.delivered is False
        release.set()
        await registry.shutdown()

    async def test_finished_tasks_expire_after_retention(self) -> None:
        clock = FakeClock()
        registry = TaskRegistry(retention_seconds=10, clock=clock)

        async def work() -> dict[str, Any]:
            return {}

        record = registry.start("work", work)
        await registry.wait(record.task_id, 5)
        clock.now += 9
        assert registry.get(record.task_id) is not None
        clock.now += 1
        assert registry.get(record.task_id) is None

    async def test_shutdown_cancels_running_tasks(self) -> None:
        registry = TaskRegistry()

        async def forever() -> dict[str, Any]:
            await asyncio.sleep(3600)
            return {}

        record = registry.start("forever", forever)
        await registry.shutdown()
        assert registry.get(record.task_id).status == "cancelled"


def _response(status: int, **kwargs: Any) -> httpx.Response:
    return httpx.Response(status, **kwargs)


class TestBridgeErrors:
    def test_string_detail_is_forwarded(self) -> None:
        assert _error_message(_response(404, json={"detail": "Resume not found"})) == "Resume not found"

    def test_validation_errors_report_fields_only(self) -> None:
        body = {
            "detail": [
                {"loc": ["body", "resume_id"], "msg": "Field required", "input": {"secret": 1}},
            ]
        }
        message = _error_message(_response(422, json=body))
        assert message == "Invalid request: resume_id: Field required"
        assert "secret" not in message

    def test_server_error_without_detail_is_generic(self) -> None:
        response = _response(500, text="Traceback (most recent call last): boom")
        assert _error_message(response) == GENERIC_SERVER_ERROR

    def test_client_error_without_detail_names_status(self) -> None:
        assert _error_message(_response(409, text="")) == "Request failed with status 409."


class TestPathSegments:
    @pytest.mark.parametrize(
        "value",
        ["3f1c2a9e-6b1d-4c3e-9a57-0e2f8b6d4c11", "abc_DEF-123", "x" * 128],
    )
    def test_ids_accepted(self, value: str) -> None:
        assert path_segment(value) == value

    @pytest.mark.parametrize(
        "value",
        ["", "../applications/bulk", "..%2F", "a?b=1", "a#frag", "a/b", "a b", "x" * 129, ".."],
    )
    def test_unsafe_values_rejected(self, value: str) -> None:
        with pytest.raises(InvalidIdentifierError, match="Invalid resume_id"):
            path_segment(value, "resume_id")

    async def test_bridge_refuses_unsafe_route_paths_without_dispatching(self) -> None:
        dispatched: list[str] = []

        async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
            dispatched.append(scope["path"])

        bridge = AppBridge(app)
        try:
            for path in ("/resumes/../applications/bulk", "/resumes/x?y=1", "/resumes/", "resumes"):
                with pytest.raises(InvalidIdentifierError):
                    await bridge.request("GET", path)
        finally:
            await bridge.aclose()
        assert dispatched == []


class TestUploadGuards:
    def test_extension_map_is_the_routers(self) -> None:
        from app.routers import resumes

        assert UPLOAD_CONTENT_TYPES is resumes.DOCUMENT_TYPES_BY_EXTENSION
        assert MAX_UPLOAD_BYTES == resumes.MAX_FILE_SIZE == 4 * 1024 * 1024

    @pytest.mark.parametrize(
        ("filename", "content_type"),
        [
            ("cv.pdf", "application/pdf"),
            ("CV.DOCX", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            ("old.doc", "application/msword"),
        ],
    )
    def test_content_type_follows_router_extension_map(self, filename: str, content_type: str) -> None:
        assert upload_content_type(filename) == content_type

    def test_unsupported_suffix_rejected(self) -> None:
        with pytest.raises(ToolError, match="Unsupported file type '.txt'"):
            upload_content_type("resume.txt")

    def test_size_limit_is_four_megabytes(self) -> None:
        check_upload_size(MAX_UPLOAD_BYTES)
        with pytest.raises(ToolError, match="upload limit is 4 MB"):
            check_upload_size(MAX_UPLOAD_BYTES + 1)
        with pytest.raises(ToolError, match="empty"):
            check_upload_size(0)


class TestWaitPolicy:
    def _runtime(self, transport: str) -> MCPRuntime:
        return MCPRuntime(bridge=None, transport=transport)  # type: ignore[arg-type]

    def test_transport_defaults(self) -> None:
        assert self._runtime("stdio").resolve_wait_seconds(None) == STDIO_DEFAULT_WAIT_SECONDS
        assert self._runtime("http").resolve_wait_seconds(None) == HTTP_DEFAULT_WAIT_SECONDS

    def test_http_max_leaves_proxy_margin(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.config import settings

        monkeypatch.setattr(settings, "request_timeout_seconds", 240)
        assert self._runtime("http").resolve_wait_seconds(10_000) == 220
        assert self._runtime("http").resolve_wait_seconds(-5) == 0


class RecordingContext:
    """Stands in for the SDK Context; records progress heartbeats."""

    def __init__(self) -> None:
        self.progress: list[tuple[float, float | None, str | None]] = []

    async def report_progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        self.progress.append((progress, total, message))


class TestRunLongOperation:
    def _runtime(self) -> MCPRuntime:
        runtime = MCPRuntime(bridge=None, transport="stdio")  # type: ignore[arg-type]
        runtime.heartbeat_interval_seconds = 0.05
        return runtime

    async def test_fast_operation_returns_result_with_task_id(self) -> None:
        async def work() -> dict[str, Any]:
            return {"value": 1}

        result = await self._runtime().run_long_operation("work", work, 5)
        assert result["status"] == "succeeded"
        assert result["value"] == 1
        assert result["task_id"]

    async def test_slow_operation_heartbeats_then_returns_handle(self) -> None:
        runtime = self._runtime()
        ctx = RecordingContext()
        release = asyncio.Event()

        async def work() -> dict[str, Any]:
            await release.wait()
            return {"value": 2}

        result = await runtime.run_long_operation("slow", work, 0.3, ctx)  # type: ignore[arg-type]
        assert result["status"] == "running"
        assert ctx.progress, "expected at least one heartbeat"
        assert ctx.progress[0][2] == "slow still running"
        release.set()
        settled = await runtime.tasks.wait(result["task_id"], 5)
        assert settled.result == {"value": 2}
        await runtime.tasks.shutdown()

    async def test_failed_operation_raises_tool_error(self) -> None:
        async def work() -> dict[str, Any]:
            raise ToolError("Job description not found")

        with pytest.raises(ToolError, match="Job description not found"):
            await self._runtime().run_long_operation("work", work, 5)


class TestFormatting:
    def test_markdown_and_summary_from_structured_resume(self, sample_resume: dict[str, Any]) -> None:
        data = {
            "resume_id": "r1",
            "raw_resume": {"content": "raw", "processing_status": "ready"},
            "processed_resume": sample_resume,
            "parent_id": "master",
            "title": "Backend role",
        }
        markdown = resume_markdown(data)
        assert markdown.startswith("# Jane Doe\n")
        assert "### Senior Backend Engineer - Acme Corp" in markdown
        assert "- Built REST APIs serving 50K requests/day using Python and FastAPI" in markdown
        assert "**Technical Skills:** Python, FastAPI" in markdown

        summary = resume_summary(data)
        assert summary["is_tailored"] is True
        assert summary["counts"]["work_experience"] == 2
        assert summary["name"] == "Jane Doe"

    def test_markdown_falls_back_to_raw_content(self) -> None:
        data = {"raw_resume": {"content": "# Raw upload\n"}, "processed_resume": None}
        assert resume_markdown(data) == "# Raw upload"


class TestInstanceId:
    def test_created_once_and_stable(self, tmp_path: Path) -> None:
        first = get_db_instance_id(tmp_path)
        assert get_db_instance_id(tmp_path) == first
        assert (tmp_path / INSTANCE_ID_FILENAME).read_text() == first

    def test_distinct_directories_get_distinct_ids(self, tmp_path: Path) -> None:
        assert get_db_instance_id(tmp_path / "a") != get_db_instance_id(tmp_path / "b")

    def test_corrupt_file_is_replaced(self, tmp_path: Path) -> None:
        (tmp_path / INSTANCE_ID_FILENAME).write_text("not-a-uuid")
        replaced = get_db_instance_id(tmp_path)
        assert replaced != "not-a-uuid"
        assert get_db_instance_id(tmp_path) == replaced

    def test_undecodable_file_is_replaced(self, tmp_path: Path) -> None:
        (tmp_path / INSTANCE_ID_FILENAME).write_bytes(b"\xff\xfe\x00garbage")
        replaced = get_db_instance_id(tmp_path)
        assert (tmp_path / INSTANCE_ID_FILENAME).read_text() == replaced

    def test_cached_after_first_read(self, tmp_path: Path) -> None:
        first = get_db_instance_id(tmp_path)
        (tmp_path / INSTANCE_ID_FILENAME).write_text("00000000-0000-4000-8000-000000000000")
        assert get_db_instance_id(tmp_path) == first

    def test_without_create_missing_id_is_none(self, tmp_path: Path) -> None:
        assert get_db_instance_id(tmp_path, create=False) is None
        assert not (tmp_path / INSTANCE_ID_FILENAME).exists()
        created = get_db_instance_id(tmp_path)
        assert get_db_instance_id(tmp_path, create=False) == created

    def test_database_established_tracks_sqlite_file(self, tmp_path: Path) -> None:
        assert database_established(tmp_path) is False
        (tmp_path / "resume_matcher.db").write_bytes(b"")
        assert database_established(tmp_path) is True

    def test_empty_file_is_replaced_after_retries(self, tmp_path: Path) -> None:
        (tmp_path / INSTANCE_ID_FILENAME).write_text("")
        instance_id = get_db_instance_id(tmp_path)
        assert (tmp_path / INSTANCE_ID_FILENAME).read_text() == instance_id

    def test_concurrent_threads_converge_on_one_id(self, tmp_path: Path) -> None:
        import app.instance_id as instance_id_module

        workers = 16
        barrier = threading.Barrier(workers)
        results: list[str | None] = []
        lock = threading.Lock()

        def create() -> None:
            barrier.wait()
            # Bypass the shared in-process cache so every thread hits the disk.
            instance_id_module._cache.pop(tmp_path, None)
            value = get_db_instance_id(tmp_path)
            with lock:
                results.append(value)

        threads = [threading.Thread(target=create) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        on_disk = (tmp_path / INSTANCE_ID_FILENAME).read_text()
        assert set(results) == {on_disk}
        assert not list(tmp_path.glob(".instance_id.*.tmp"))

    def test_concurrent_processes_converge_on_one_id(self, tmp_path: Path) -> None:
        context = multiprocessing.get_context("spawn")
        with ProcessPoolExecutor(max_workers=4, mp_context=context) as pool:
            results = list(pool.map(get_db_instance_id, [tmp_path] * 8))
        assert set(results) == {(tmp_path / INSTANCE_ID_FILENAME).read_text()}

    def test_defaults_to_settings_data_dir(self) -> None:
        from app.config import settings

        instance_id = get_db_instance_id()
        assert (settings.data_dir / INSTANCE_ID_FILENAME).read_text() == instance_id
