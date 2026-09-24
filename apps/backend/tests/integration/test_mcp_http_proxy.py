"""MCP over HTTP through the Next.js proxy (opt-in end-to-end test).

Skipped unless ``MCP_E2E_BASE_URL`` points at a running frontend (for example
``http://localhost:3000``) whose ``/api`` rewrite reaches a backend started
with ``MCP_HTTP_ENABLED=1`` and ``MCP_AUTH_TOKEN`` equal to ``MCP_E2E_TOKEN``.
Run with::

    MCP_E2E_BASE_URL=http://localhost:3000 MCP_E2E_TOKEN=<token> \\
        uv run pytest -m e2e tests/integration/test_mcp_http_proxy.py

The default-wait check needs at least one stored resume and an LLM provider
that either answers or hangs (an unresponsive endpoint exercises the
task-handle path); it cancels the task it starts.
"""

import json
import os
import socket
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

BASE_URL = os.environ.get("MCP_E2E_BASE_URL", "").rstrip("/")
TOKEN = os.environ.get("MCP_E2E_TOKEN", "")
MCP_URL = f"{BASE_URL}/api/v1/mcp"
MODERN = "2026-07-28"
DEFAULT_WAIT_BUDGET_SECONDS = 50.0
ENVELOPE = {
    "io.modelcontextprotocol/protocolVersion": MODERN,
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "proxy-e2e", "version": "1.0"},
}

_SOCKET_CONNECT = socket.socket.connect
_SOCKET_CONNECT_EX = socket.socket.connect_ex
_CREATE_CONNECTION = socket.create_connection

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not BASE_URL or not TOKEN,
        reason="set MCP_E2E_BASE_URL and MCP_E2E_TOKEN to run the proxy end-to-end test",
    ),
]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, deny_external_network: Any) -> Iterator[httpx.Client]:
    """A real-socket client; redirects are surfaced, never followed."""
    del deny_external_network
    monkeypatch.setattr(socket.socket, "connect", _SOCKET_CONNECT)
    monkeypatch.setattr(socket.socket, "connect_ex", _SOCKET_CONNECT_EX)
    monkeypatch.setattr(socket, "create_connection", _CREATE_CONNECTION)
    with httpx.Client(follow_redirects=False, timeout=120) as http:
        yield http


def headers(method: str, name: str | None = None, **extra: str) -> dict[str, str]:
    """Headers of a 2026-07-28 single-exchange POST."""
    result = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": MODERN,
        "Mcp-Method": method,
    }
    if name is not None:
        result["Mcp-Name"] = name
    result.update(extra)
    return result


def body(request_id: int, method: str, **params: Any) -> dict[str, Any]:
    """A 2026-07-28 request carrying the per-request ``_meta`` envelope."""
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": {"_meta": ENVELOPE, **params}}


def call_tool(client: httpx.Client, request_id: int, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call a tool on the modern path and decode its JSON payload."""
    response = client.post(
        MCP_URL,
        json=body(request_id, "tools/call", name=name, arguments=arguments),
        headers=headers("tools/call", name=name),
    )
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def test_modern_discover_and_tools_list_without_redirects(client: httpx.Client) -> None:
    discover = client.post(MCP_URL, json=body(1, "server/discover"), headers=headers("server/discover"))
    listed = client.post(MCP_URL, json=body(2, "tools/list"), headers=headers("tools/list"))

    for response in (discover, listed):
        assert response.status_code == 200, (response.status_code, response.headers, response.text)
        assert "mcp-session-id" not in response.headers
        assert response.headers["content-type"].startswith("application/json")
        assert response.json()["result"]["resultType"] == "complete"
    assert MODERN in discover.json()["result"]["supportedVersions"]
    tools = listed.json()["result"]
    assert tools["ttlMs"] == 3_600_000
    assert [tool["name"] for tool in tools["tools"]][0] == "get_status"


def test_routing_headers_reach_backend_intact(client: httpx.Client) -> None:
    """The backend validates Mcp-Method/Mcp-Name, so mismatches prove delivery."""
    matched = client.post(
        MCP_URL,
        json=body(1, "tools/call", name="list_resumes", arguments={}),
        headers=headers("tools/call", name="list_resumes"),
    )
    wrong_method = client.post(MCP_URL, json=body(2, "tools/list"), headers=headers("server/discover"))
    wrong_name = client.post(
        MCP_URL,
        json=body(3, "tools/call", name="list_resumes", arguments={}),
        headers=headers("tools/call", name="get_status"),
    )

    assert matched.status_code == 200, matched.text
    assert "mcp-session-id" not in matched.headers
    assert matched.json()["result"]["resultType"] == "complete"
    for rejected in (wrong_method, wrong_name):
        assert rejected.status_code == 400, rejected.text
        assert rejected.json()["error"]["code"] == -32020


def test_foreign_origin_and_missing_token_rejected(client: httpx.Client) -> None:
    foreign = client.post(
        MCP_URL,
        json=body(1, "server/discover"),
        headers=headers("server/discover", Origin="https://evil.example"),
    )
    anonymous_headers = headers("server/discover")
    del anonymous_headers["Authorization"]
    anonymous = client.post(MCP_URL, json=body(2, "server/discover"), headers=anonymous_headers)

    assert foreign.status_code == 403, foreign.text
    assert anonymous.status_code == 401, anonymous.text


def test_default_wait_call_responds_within_budget(client: httpx.Client) -> None:
    resumes = call_tool(client, 1, "list_resumes", {})["resumes"]
    assert resumes, "the e2e instance needs at least one stored resume"
    resume_id = resumes[0]["resume_id"]
    job_id = call_tool(
        client,
        2,
        "add_jobs",
        {"descriptions": ["Senior Python engineer: FastAPI, PostgreSQL, distributed systems."]},
    )["job_ids"][0]

    started = time.monotonic()
    result = call_tool(client, 3, "tailor_resume_preview", {"resume_id": resume_id, "job_id": job_id})
    elapsed = time.monotonic() - started

    assert elapsed < DEFAULT_WAIT_BUDGET_SECONDS, elapsed
    assert result["status"] in ("succeeded", "running"), result
    assert result["task_id"]
    if result["status"] == "running":
        call_tool(client, 4, "cancel_task", {"task_id": result["task_id"]})
