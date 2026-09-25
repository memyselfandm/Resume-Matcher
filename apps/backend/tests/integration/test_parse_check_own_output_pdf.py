"""Real-Chromium end-to-end parse check of all seven templates (opt-in).

Run with ``uv run pytest -m pdf``. Requires Playwright's Chromium and the
Next.js frontend at ``FRONTEND_BASE_URL`` (default ``http://localhost:3000``)
whose server-side data origin is ``http://127.0.0.1:8000``; port 8000 must be
free because this test serves the backend there, against its isolated test
database, for the print page to fetch the resume from.
"""

import asyncio
import json
import socket
from typing import Any

import httpx
import pytest
import uvicorn
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.main import app
from app.pdf import close_pdf_renderer
from app.services.ats_parse.templates import TEMPLATE_IDS, TEMPLATE_LAYOUTS
from tests.ats_parse_renders import source as render_source

pytestmark = pytest.mark.pdf

# Captured at import, before the autouse network guard replaces them.
_REAL_SOCKET = {
    "create_connection": socket.create_connection,
    "connect": socket.socket.connect,
    "connect_ex": socket.socket.connect_ex,
}


@pytest.fixture
def real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "create_connection", _REAL_SOCKET["create_connection"])
    monkeypatch.setattr(socket.socket, "connect", _REAL_SOCKET["connect"])
    monkeypatch.setattr(socket.socket, "connect_ex", _REAL_SOCKET["connect_ex"])


async def _serve_backend() -> tuple[uvicorn.Server, asyncio.Task[None]]:
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=8000, lifespan="off", log_level="warning")
    )
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            return server, task
        if task.done():
            task.result()
        await asyncio.sleep(0.05)
    raise RuntimeError("Backend did not start on 127.0.0.1:8000")


async def test_all_templates_render_and_parse_check(real_network: None, isolated_db: Any) -> None:
    try:
        async with httpx.AsyncClient(timeout=10) as probe:
            await probe.get(settings.frontend_base_url)
    except httpx.HTTPError as exc:
        pytest.fail(f"The pdf marker needs the frontend at {settings.frontend_base_url}: {exc}")

    source = render_source()
    resume = await isolated_db.create_resume(
        content=json.dumps(source),
        content_type="json",
        processed_data=source,
        processing_status="ready",
    )
    server, task = await _serve_backend()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", timeout=None
        ) as client:
            response = await client.post(
                f"/api/v1/resumes/{resume['resume_id']}/parse-check",
                json={"all_templates": True, "content_language": "en"},
            )
    finally:
        server.should_exit = True
        await task
        await close_pdf_renderer()

    assert response.status_code == 200
    results = {result["template"]: result for result in response.json()["results"]}
    assert list(results) == list(TEMPLATE_IDS)
    for template, result in results.items():
        assert result["status"] == "ok", template
        checks = {check["id"]: check for check in result["report"]["checks"]}
        expected = "fail" if TEMPLATE_LAYOUTS[template].two_column else "pass"
        assert checks["multi_column"]["status"] == expected, template
    for template in ("swiss-single", "modern", "latex"):
        roundtrip = results[template]["report"]["roundtrip"]
        assert roundtrip["content_recall"] >= 0.95, template
        assert roundtrip["order_fidelity"] >= 0.95, template
