"""stdio smoke tests: the real entry point in a subprocess, one per protocol era.

The first request on a stdio connection fixes its era, so the 2026-07-28
(``server/discover`` + per-request ``_meta`` envelope) and 2025-11-25
(``initialize`` handshake) flows each get their own process. Every stdout line
must be a JSON-RPC message; logs belong on stderr.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[2]
MODERN = "2026-07-28"
LEGACY = "2025-11-25"
ENVELOPE = {
    "io.modelcontextprotocol/protocolVersion": MODERN,
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "stdio-smoke", "version": "1.0"},
}
RESPONSE_TIMEOUT_SECONDS = 60.0


class StdioServer:
    """Line-oriented JSON-RPC driver for ``python -m app.mcp``."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self.stdout_lines: list[str] = []

    @classmethod
    async def start(cls, data_dir: Path, cwd: Path) -> "StdioServer":
        env = {
            **os.environ,
            "DATA_DIR": str(data_dir),
            "PYTHONPATH": str(BACKEND_DIR),
            "LOG_LEVEL": "INFO",
        }
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "app.mcp",
            cwd=cwd,  # keep a developer .env out of the child's settings
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        return cls(process)

    async def _send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        await self.process.stdin.drain()

    async def request(self, request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        assert self.process.stdout is not None
        while True:
            raw = await asyncio.wait_for(self.process.stdout.readline(), RESPONSE_TIMEOUT_SECONDS)
            assert raw, f"server closed stdout; stderr:\n{await self._stderr()}"
            line = raw.decode()
            self.stdout_lines.append(line)
            message = json.loads(line)
            if message.get("id") == request_id:
                return message

    async def notify(self, method: str) -> None:
        await self._send({"jsonrpc": "2.0", "method": method})

    async def _stderr(self) -> str:
        assert self.process.stderr is not None
        return (await self.process.stderr.read()).decode(errors="replace")

    async def close(self) -> tuple[int, str]:
        """Close stdin (EOF), collect remaining output, return (code, stderr)."""
        assert self.process.stdin is not None
        self.process.stdin.close()
        stdout, stderr = await asyncio.wait_for(self.process.communicate(), RESPONSE_TIMEOUT_SECONDS)
        self.stdout_lines.extend(line for line in stdout.decode().splitlines(keepends=True) if line.strip())
        return self.process.returncode or 0, stderr.decode(errors="replace")

    def assert_stdout_is_jsonrpc(self) -> None:
        assert self.stdout_lines
        for line in self.stdout_lines:
            message = json.loads(line)
            assert message.get("jsonrpc") == "2.0", line


async def test_modern_client_discovers_and_calls_tools(tmp_path: Path) -> None:
    server = await StdioServer.start(tmp_path / "data", tmp_path)
    try:
        discover = await server.request(1, "server/discover", {"_meta": ENVELOPE})
        listed = await server.request(2, "tools/list", {"_meta": ENVELOPE})
        called = await server.request(
            3,
            "tools/call",
            {"_meta": ENVELOPE, "name": "list_resumes", "arguments": {}},
        )
        missing = await server.request(4, "resources/read", {"_meta": ENVELOPE, "uri": "resume://missing"})
    finally:
        code, stderr = await server.close()

    assert code == 0, stderr
    server.assert_stdout_is_jsonrpc()

    assert MODERN in discover["result"]["supportedVersions"]
    for response in (discover, listed, called):
        assert response["result"]["resultType"] == "complete"

    tools = listed["result"]
    assert tools["ttlMs"] == 3_600_000
    assert tools["cacheScope"] == "private"
    names = [tool["name"] for tool in tools["tools"]]
    assert names[0] == "get_status"
    assert "tailor_resume_confirm" in names

    assert called["result"]["isError"] is False
    assert json.loads(called["result"]["content"][0]["text"]) == {"resumes": []}

    assert missing["error"]["code"] == -32602


async def test_handshake_client_initializes_and_lists_tools(tmp_path: Path) -> None:
    server = await StdioServer.start(tmp_path / "data", tmp_path)
    try:
        initialized = await server.request(
            1,
            "initialize",
            {
                "protocolVersion": LEGACY,
                "capabilities": {},
                "clientInfo": {"name": "stdio-smoke", "version": "1.0"},
            },
        )
        await server.notify("notifications/initialized")
        listed = await server.request(2, "tools/list", {})
    finally:
        code, stderr = await server.close()

    assert code == 0, stderr
    server.assert_stdout_is_jsonrpc()
    assert initialized["result"]["protocolVersion"] == LEGACY
    assert initialized["result"]["serverInfo"]["name"] == "resume-matcher"
    names = [tool["name"] for tool in listed["result"]["tools"]]
    assert names[0] == "get_status"
    # Cache hints are 2026-07-28 vocabulary and are sieved from older eras.
    assert "ttlMs" not in listed["result"]
