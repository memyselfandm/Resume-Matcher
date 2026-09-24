"""MCP Streamable HTTP transport at ``/api/v1/mcp``.

Every test drives the real FastAPI app through its real lifespan (which builds
the MCP server and session manager) and speaks raw JSON-RPC over HTTP, so the
exact route, bearer auth, transport security, body limit and era routing are
all exercised end to end in-process. Settings are driven from environment
variables and applied to the live ``settings`` object without reloading any
module, the same way a restarted process would read them.
"""

import base64
import copy
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from starlette.testclient import TestClient

from app.config import Settings, settings
from app.database import Database
from app.main import app
from app.mcp.http import (
    MAX_REQUEST_BODY_BYTES,
    MCP_HTTP_PATH,
    MCPHTTPConfigurationError,
    _host_allowed,
)

TOKEN = "mcp-test-token-5f0c2d7e9a41b3c68d20e7f1"
MODERN = "2026-07-28"
LEGACY = "2025-11-25"
BACKEND_ORIGIN = "http://127.0.0.1:8000"
UNICODE_TOKEN = "jeton-unicode-0123456789abcdef-é"
ENVELOPE = {
    "io.modelcontextprotocol/protocolVersion": MODERN,
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "http-test", "version": "1.0"},
}
MCP_SETTING_NAMES = (
    "mcp_http_enabled",
    "mcp_auth_token",
    "mcp_allow_no_auth",
    "mcp_allowed_hosts",
    "mcp_allowed_origins",
    "mcp_allowed_forwarded_hosts",
)
MCP_ENV_NAMES = tuple(name.upper() for name in MCP_SETTING_NAMES)


def apply_env(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    """Set MCP environment variables and load them into the live settings."""
    for name in MCP_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    fresh = Settings(_env_file=None)
    for name in MCP_SETTING_NAMES:
        monkeypatch.setattr(settings, name, getattr(fresh, name))


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """MCP over HTTP enabled with a bearer token."""
    apply_env(monkeypatch, MCP_HTTP_ENABLED="1", MCP_AUTH_TOKEN=TOKEN)


def modern_headers(method: str, name: str | None = None, **extra: str) -> dict[str, str]:
    """Headers of a 2026-07-28 single-exchange POST."""
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": MODERN,
        "Mcp-Method": method,
    }
    if name is not None:
        headers["Mcp-Name"] = name
    headers.update(extra)
    return headers


def legacy_headers(protocol_version: str | None = None) -> dict[str, str]:
    """Headers of a handshake-era POST."""
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if protocol_version is not None:
        headers["MCP-Protocol-Version"] = protocol_version
    return headers


def modern_body(request_id: int, method: str, **params: Any) -> dict[str, Any]:
    """A 2026-07-28 request carrying the per-request ``_meta`` envelope."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": {"_meta": ENVELOPE, **params},
    }


INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": LEGACY,
        "capabilities": {},
        "clientInfo": {"name": "http-test", "version": "1.0"},
    },
}


@asynccontextmanager
async def served(base_url: str = BACKEND_ORIGIN) -> AsyncIterator[httpx.AsyncClient]:
    """Run one app lifespan and yield an in-process HTTP client for it."""
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=base_url, timeout=30
        ) as client:
            yield client


def assert_modern_discover(response: httpx.Response) -> None:
    """A successful 2026-07-28 ``server/discover`` served without a session."""
    assert response.status_code == 200, response.text
    assert "mcp-session-id" not in response.headers
    result = response.json()["result"]
    assert MODERN in result["supportedVersions"]
    assert result["resultType"] == "complete"


def assert_legacy_initialize(response: httpx.Response) -> None:
    """A successful handshake-era ``initialize`` over stateless HTTP."""
    assert response.status_code == 200, response.text
    assert "mcp-session-id" not in response.headers
    result = response.json()["result"]
    assert result["protocolVersion"] == LEGACY
    assert result["serverInfo"]["name"] == "resume-matcher"


class TestGating:
    async def test_disabled_by_default_returns_404(self, monkeypatch: pytest.MonkeyPatch) -> None:
        apply_env(monkeypatch)
        assert settings.mcp_http_enabled is False
        async with served() as client:
            response = await client.post(
                MCP_HTTP_PATH, json=modern_body(1, "server/discover"), headers=modern_headers("server/discover")
            )
        assert response.status_code == 404

    async def test_route_answers_404_outside_a_lifespan(self, enabled: None) -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=BACKEND_ORIGIN
        ) as client:
            response = await client.post(MCP_HTTP_PATH, json=INITIALIZE, headers=legacy_headers())
        assert response.status_code == 404

    async def test_enabled_without_token_fails_startup(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        apply_env(monkeypatch, MCP_HTTP_ENABLED="1")
        with caplog.at_level(logging.ERROR, logger="app.mcp.http"):
            with pytest.raises(MCPHTTPConfigurationError, match="MCP_AUTH_TOKEN"):
                async with served():
                    pass
        assert "MCP_AUTH_TOKEN is empty" in caplog.text

    async def test_whitespace_token_counts_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        apply_env(monkeypatch, MCP_HTTP_ENABLED="1", MCP_AUTH_TOKEN="   ")
        with pytest.raises(MCPHTTPConfigurationError):
            async with served():
                pass

    async def test_short_token_fails_startup(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        apply_env(monkeypatch, MCP_HTTP_ENABLED="1", MCP_AUTH_TOKEN="x" * 31)
        with caplog.at_level(logging.ERROR, logger="app.mcp.http"):
            with pytest.raises(MCPHTTPConfigurationError, match="openssl rand -hex 32"):
                async with served():
                    pass
        assert "too short (31 characters)" in caplog.text
        assert "x" * 31 not in caplog.text

    async def test_invalid_settings_fail_before_data_migrations(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        apply_env(monkeypatch, MCP_HTTP_ENABLED="1")
        with (
            patch("app.scripts.migrate_tinydb_to_sqlite.migrate", new_callable=AsyncMock) as migrate,
            patch("app.config.migrate_legacy_keys") as migrate_keys,
        ):
            with pytest.raises(MCPHTTPConfigurationError):
                async with served():
                    pass
        migrate.assert_not_awaited()
        migrate_keys.assert_not_called()

    async def test_mcp_teardown_failure_still_runs_cleanup(self, enabled: None) -> None:
        @asynccontextmanager
        async def failing_teardown(_app: Any) -> AsyncIterator[None]:
            yield
            raise RuntimeError("MCP teardown failed")

        database = MagicMock()
        database.close = AsyncMock()
        with (
            patch("app.main.mcp_http_lifespan", failing_teardown),
            patch("app.main.drain_processing_cleanup_tasks", new_callable=AsyncMock) as drain,
            patch("app.main.close_pdf_renderer", new_callable=AsyncMock) as close_pdf,
            patch("app.main.db", database),
        ):
            with pytest.raises(RuntimeError, match="MCP teardown failed"):
                async with app.router.lifespan_context(app):
                    pass
        drain.assert_awaited_once()
        close_pdf.assert_awaited_once()
        database.close.assert_awaited_once()

    async def test_allow_no_auth_serves_with_warnings(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        apply_env(monkeypatch, MCP_HTTP_ENABLED="1", MCP_ALLOW_NO_AUTH="1")
        headers = modern_headers("server/discover")
        del headers["Authorization"]
        with caplog.at_level(logging.WARNING, logger="app.mcp.http"):
            async with served() as client:
                startup_warnings = [r for r in caplog.records if "WITHOUT authentication" in r.message]
                response = await client.post(
                    MCP_HTTP_PATH, json=modern_body(1, "server/discover"), headers=headers
                )
        assert_modern_discover(response)
        assert len(startup_warnings) == 1
        assert any("unauthenticated MCP HTTP request" in r.message for r in caplog.records)

    async def test_setting_toggle_flips_endpoint_without_reload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        request = modern_body(1, "server/discover")
        headers = modern_headers("server/discover")

        apply_env(monkeypatch, MCP_HTTP_ENABLED="1", MCP_AUTH_TOKEN=TOKEN)
        async with served() as client:
            assert_modern_discover(await client.post(MCP_HTTP_PATH, json=request, headers=headers))

        apply_env(monkeypatch, MCP_HTTP_ENABLED="0", MCP_AUTH_TOKEN=TOKEN)
        async with served() as client:
            assert (await client.post(MCP_HTTP_PATH, json=request, headers=headers)).status_code == 404

        apply_env(monkeypatch, MCP_HTTP_ENABLED="true", MCP_AUTH_TOKEN=TOKEN)
        async with served() as client:
            assert_modern_discover(await client.post(MCP_HTTP_PATH, json=request, headers=headers))


class TestAuth:
    @pytest.mark.parametrize(
        "authorization",
        [None, "Bearer wrong-token", f"Basic {TOKEN}", f"Bearer {TOKEN}x", "Bearer ", TOKEN],
        ids=["missing", "wrong", "basic-scheme", "suffix", "empty", "no-scheme"],
    )
    async def test_bad_credentials_rejected_before_sdk(
        self, enabled: None, authorization: str | None, caplog: pytest.LogCaptureFixture
    ) -> None:
        headers = modern_headers("server/discover")
        del headers["Authorization"]
        if authorization is not None:
            headers["Authorization"] = authorization
        with caplog.at_level(logging.DEBUG):
            async with served() as client:
                response = await client.post(
                    MCP_HTTP_PATH, json=modern_body(1, "server/discover"), headers=headers
                )
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert TOKEN not in caplog.text

    async def test_trailing_slash_is_served_without_redirect(self, enabled: None) -> None:
        path = MCP_HTTP_PATH + "/"
        anonymous = modern_headers("server/discover")
        del anonymous["Authorization"]
        async with served() as client:
            unauthenticated = await client.post(
                path, json=modern_body(1, "server/discover"), headers=anonymous
            )
            authenticated = await client.post(
                path, json=modern_body(2, "server/discover"), headers=modern_headers("server/discover")
            )
        assert unauthenticated.status_code == 401
        assert "location" not in unauthenticated.headers
        assert_modern_discover(authenticated)

    async def test_non_ascii_token_compares_as_utf8(self, monkeypatch: pytest.MonkeyPatch) -> None:
        apply_env(monkeypatch, MCP_HTTP_ENABLED="1", MCP_AUTH_TOKEN=UNICODE_TOKEN)
        good: dict[str, Any] = modern_headers("server/discover")
        good["Authorization"] = f"Bearer {UNICODE_TOKEN}".encode()
        async with served() as client:
            ok = await client.post(MCP_HTTP_PATH, json=modern_body(1, "server/discover"), headers=good)
            bad = await client.post(
                MCP_HTTP_PATH,
                json=modern_body(2, "server/discover"),
                headers={**good, "Authorization": f"Bearer {UNICODE_TOKEN[:-1]}e"},
            )
        assert_modern_discover(ok)
        assert bad.status_code == 401

    async def test_token_is_never_logged(
        self, enabled: None, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            async with served() as client:
                await client.post(
                    MCP_HTTP_PATH, json=modern_body(1, "server/discover"), headers=modern_headers("server/discover")
                )
        assert TOKEN not in caplog.text
        assert TOKEN not in repr(settings)


class TestBothEras:
    async def test_modern_discover_and_legacy_initialize(self, enabled: None) -> None:
        async with served() as client:
            discover = await client.post(
                MCP_HTTP_PATH, json=modern_body(1, "server/discover"), headers=modern_headers("server/discover")
            )
            initialize = await client.post(MCP_HTTP_PATH, json=INITIALIZE, headers=legacy_headers())
            legacy_list = await client.post(
                MCP_HTTP_PATH,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                headers=legacy_headers(LEGACY),
            )
        assert_modern_discover(discover)
        assert_legacy_initialize(initialize)
        assert legacy_list.status_code == 200
        assert "mcp-session-id" not in legacy_list.headers
        assert "ttlMs" not in legacy_list.json()["result"]
        assert legacy_list.json()["result"]["tools"][0]["name"] == "get_status"

    async def test_modern_tools_list_and_call(self, enabled: None, isolated_db: Database) -> None:
        async with served() as client:
            listed = await client.post(
                MCP_HTTP_PATH, json=modern_body(1, "tools/list"), headers=modern_headers("tools/list")
            )
            called = await client.post(
                MCP_HTTP_PATH,
                json=modern_body(2, "tools/call", name="list_resumes", arguments={}),
                headers=modern_headers("tools/call", name="list_resumes"),
            )
        assert listed.status_code == 200
        assert "mcp-session-id" not in listed.headers
        tools = listed.json()["result"]
        assert tools["resultType"] == "complete"
        assert tools["ttlMs"] == 3_600_000
        assert tools["tools"][0]["name"] == "get_status"
        assert called.status_code == 200
        assert "mcp-session-id" not in called.headers
        assert called.json()["result"]["isError"] is False

    async def test_mismatched_mcp_method_is_header_mismatch(self, enabled: None) -> None:
        async with served() as client:
            response = await client.post(
                MCP_HTTP_PATH, json=modern_body(1, "tools/list"), headers=modern_headers("server/discover")
            )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == -32020

    async def test_mismatched_mcp_name_is_header_mismatch(self, enabled: None) -> None:
        async with served() as client:
            response = await client.post(
                MCP_HTTP_PATH,
                json=modern_body(1, "tools/call", name="list_resumes", arguments={}),
                headers=modern_headers("tools/call", name="get_status"),
            )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == -32020

    async def test_http_runtime_refuses_local_paths(self, enabled: None, tmp_path: Any) -> None:
        resume_file = tmp_path / "resume.pdf"
        resume_file.write_bytes(b"%PDF-1.4 fake")
        async with served() as client:
            response = await client.post(
                MCP_HTTP_PATH,
                json=modern_body(1, "tools/call", name="upload_resume", arguments={"path": str(resume_file)}),
                headers=modern_headers("tools/call", name="upload_resume"),
            )
        result = response.json()["result"]
        assert result["isError"] is True
        assert "only available on the stdio transport" in result["content"][0]["text"]


def test_two_sequential_testclient_lifespans(enabled: None) -> None:
    """Each lifespan builds a fresh manager; the route must follow it."""
    for _ in range(2):
        with TestClient(app, base_url=BACKEND_ORIGIN) as client:
            discover = client.post(
                MCP_HTTP_PATH, json=modern_body(1, "server/discover"), headers=modern_headers("server/discover")
            )
            initialize = client.post(MCP_HTTP_PATH, json=INITIALIZE, headers=legacy_headers())
        assert_modern_discover(discover)
        assert_legacy_initialize(initialize)
    assert app.state.mcp_session_manager is None


class TestTransportSecurity:
    async def test_foreign_host_rejected(self, enabled: None) -> None:
        async with served(base_url="http://evil.example:8000") as client:
            response = await client.post(
                MCP_HTTP_PATH, json=modern_body(1, "server/discover"), headers=modern_headers("server/discover")
            )
        assert response.status_code == 421

    async def test_foreign_host_rejected_on_legacy_path(self, enabled: None) -> None:
        async with served(base_url="http://evil.example") as client:
            response = await client.post(MCP_HTTP_PATH, json=INITIALIZE, headers=legacy_headers())
        assert response.status_code == 421

    @pytest.mark.parametrize("origin", ["https://evil.example", "http://localhost.evil.example:3000"])
    async def test_foreign_origin_rejected(self, enabled: None, origin: str) -> None:
        async with served() as client:
            modern = await client.post(
                MCP_HTTP_PATH,
                json=modern_body(1, "server/discover"),
                headers=modern_headers("server/discover", Origin=origin),
            )
            legacy = await client.post(
                MCP_HTTP_PATH, json=INITIALIZE, headers={**legacy_headers(), "Origin": origin}
            )
        assert modern.status_code == 403
        assert legacy.status_code == 403

    async def test_ipv6_loopback_allowed_by_default(self, enabled: None) -> None:
        async with served(base_url="http://[::1]:8000") as client:
            response = await client.post(
                MCP_HTTP_PATH,
                json=modern_body(1, "server/discover"),
                headers=modern_headers("server/discover", Origin="http://[::1]:3000"),
            )
        assert_modern_discover(response)

    async def test_local_origin_allowed(self, enabled: None) -> None:
        async with served() as client:
            response = await client.post(
                MCP_HTTP_PATH,
                json=modern_body(1, "server/discover"),
                headers=modern_headers("server/discover", Origin="http://localhost:3000"),
            )
        assert_modern_discover(response)

    async def test_configured_origin_and_host_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        apply_env(
            monkeypatch,
            MCP_HTTP_ENABLED="1",
            MCP_AUTH_TOKEN=TOKEN,
            MCP_ALLOWED_HOSTS="resume.internal:*, 127.0.0.1:*",
            MCP_ALLOWED_ORIGINS="https://resume.internal",
        )
        async with served(base_url="http://resume.internal:8000") as client:
            allowed = await client.post(
                MCP_HTTP_PATH,
                json=modern_body(1, "server/discover"),
                headers=modern_headers("server/discover", Origin="https://resume.internal"),
            )
            replaced_default = await client.post(
                MCP_HTTP_PATH,
                json=modern_body(2, "server/discover"),
                headers=modern_headers("server/discover", Origin="http://localhost:3000"),
            )
        assert_modern_discover(allowed)
        assert replaced_default.status_code == 403

    async def test_forwarded_host_allow_list(self, monkeypatch: pytest.MonkeyPatch) -> None:
        apply_env(
            monkeypatch,
            MCP_HTTP_ENABLED="1",
            MCP_AUTH_TOKEN=TOKEN,
            MCP_ALLOWED_FORWARDED_HOSTS="localhost:*",
        )
        async with served() as client:
            proxied = await client.post(
                MCP_HTTP_PATH,
                json=modern_body(1, "server/discover"),
                headers=modern_headers("server/discover", **{"X-Forwarded-Host": "localhost:3000"}),
            )
            foreign = await client.post(
                MCP_HTTP_PATH,
                json=modern_body(2, "server/discover"),
                headers=modern_headers("server/discover", **{"X-Forwarded-Host": "evil.example"}),
            )
            direct = await client.post(
                MCP_HTTP_PATH, json=modern_body(3, "server/discover"), headers=modern_headers("server/discover")
            )
        assert_modern_discover(proxied)
        assert foreign.status_code == 421
        assert_modern_discover(direct)

    async def test_every_forwarded_host_value_is_checked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        apply_env(
            monkeypatch,
            MCP_HTTP_ENABLED="1",
            MCP_AUTH_TOKEN=TOKEN,
            MCP_ALLOWED_FORWARDED_HOSTS="localhost:*",
        )
        base = list(modern_headers("server/discover").items())
        async with served() as client:
            two_headers = await client.post(
                MCP_HTTP_PATH,
                json=modern_body(1, "server/discover"),
                headers=[*base, ("X-Forwarded-Host", "localhost:3000"), ("X-Forwarded-Host", "evil.example")],
            )
            comma_list = await client.post(
                MCP_HTTP_PATH,
                json=modern_body(2, "server/discover"),
                headers=[*base, ("X-Forwarded-Host", "localhost:3000, evil.example")],
            )
            all_local = await client.post(
                MCP_HTTP_PATH,
                json=modern_body(3, "server/discover"),
                headers=[*base, ("X-Forwarded-Host", "localhost:3000"), ("X-Forwarded-Host", "localhost:8080")],
            )
        assert two_headers.status_code == 421
        assert comma_list.status_code == 421
        assert_modern_discover(all_local)

    @pytest.mark.parametrize(
        ("value", "allowed"),
        [
            ("localhost:3000", True),
            ("[::1]:3000", True),
            ("resume.example", True),
            ("localhost", False),
            ("localhost:", False),
            ("localhost:evil", False),
            ("localhost:3000.evil.com", False),
            ("127.0.0.1:8000.evil.com", False),
            ("localhost:123456", False),
            ("evil.example:3000", False),
        ],
    )
    def test_host_patterns_require_numeric_port(self, value: str, allowed: bool) -> None:
        patterns = ["localhost:*", "127.0.0.1:*", "[::1]:*", "resume.example"]
        assert _host_allowed(value, patterns) is allowed

    async def test_forwarded_host_ignored_without_allow_list(self, enabled: None) -> None:
        async with served() as client:
            response = await client.post(
                MCP_HTTP_PATH,
                json=modern_body(1, "server/discover"),
                headers=modern_headers("server/discover", **{"X-Forwarded-Host": "anything.example"}),
            )
        assert_modern_discover(response)


@pytest.fixture
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


class TestBodyLimit:
    async def test_five_megabyte_base64_upload_accepted(
        self, enabled: None, isolated_db: Database, mocked_upload_parsing: AsyncMock
    ) -> None:
        content = b"%PDF-1.4 " + b"x" * 3_750_000
        encoded = base64.b64encode(content).decode()
        request = modern_body(
            1,
            "tools/call",
            name="upload_resume",
            arguments={"filename": "resume.pdf", "content_base64": encoded},
        )
        async with served() as client:
            response = await client.post(
                MCP_HTTP_PATH, json=request, headers=modern_headers("tools/call", name="upload_resume")
            )
        assert len(response.request.content) > 5_000_000
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert result["isError"] is False, result
        mocked_upload_parsing.assert_awaited_once()
        assert mocked_upload_parsing.await_args.args[0] == content
        assert (await isolated_db.get_stats())["total_resumes"] == 1

    @pytest.mark.parametrize("declare_length", [True, False], ids=["content-length", "chunked"])
    async def test_body_over_limit_rejected(
        self, enabled: None, mocked_upload_parsing: AsyncMock, declare_length: bool
    ) -> None:
        body = b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"pad":"' + b"x" * (
            MAX_REQUEST_BODY_BYTES + 1024
        ) + b'"}}'
        content: bytes | AsyncIterator[bytes] = body
        if not declare_length:

            async def chunks() -> AsyncIterator[bytes]:
                for start in range(0, len(body), 1 << 20):
                    yield body[start : start + (1 << 20)]

            content = chunks()
        async with served() as client:
            response = await client.post(
                MCP_HTTP_PATH, content=content, headers=modern_headers("tools/list")
            )
        assert response.status_code == 413
        mocked_upload_parsing.assert_not_awaited()


class TestSettingsParsing:
    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in MCP_ENV_NAMES:
            monkeypatch.delenv(name, raising=False)
        fresh = Settings(_env_file=None)
        assert fresh.mcp_http_enabled is False
        assert fresh.mcp_auth_token.get_secret_value() == ""
        assert fresh.mcp_allow_no_auth is False
        assert fresh.mcp_allowed_hosts == ["127.0.0.1:*", "localhost:*", "[::1]:*"]
        assert fresh.mcp_allowed_origins == [
            "http://localhost:*",
            "http://127.0.0.1:*",
            "http://[::1]:*",
        ]
        assert fresh.mcp_allowed_forwarded_hosts == []

    def test_comma_separated_lists(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_ALLOWED_ORIGINS", " https://a.example ,http://b.example:*,")
        monkeypatch.setenv("MCP_ALLOWED_FORWARDED_HOSTS", "resume.example.com")
        fresh = Settings(_env_file=None)
        assert fresh.mcp_allowed_origins == ["https://a.example", "http://b.example:*"]
        assert fresh.mcp_allowed_forwarded_hosts == ["resume.example.com"]

    def test_token_is_masked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_AUTH_TOKEN", TOKEN)
        fresh = Settings(_env_file=None)
        assert TOKEN not in repr(fresh)
        assert TOKEN not in str(fresh.model_dump())
