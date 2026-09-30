"""Integration tests for POST /api/v1/ats/parse-check (file upload, no persistence)."""

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers.resumes import MAX_FILE_SIZE
from app.services.ats_parse import engine

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ats_parse"
PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
DOC = "application/msword"
URL = "/api/v1/ats/parse-check"


@pytest.fixture
def client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _file(name: str, content_type: str) -> dict[str, tuple[str, bytes, str]]:
    return {"file": (name, (FIXTURES / name).read_bytes(), content_type)}


def _checks(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {check["id"]: check for check in body["checks"]}


async def test_pdf_report_contract(client: AsyncClient) -> None:
    async with client:
        response = await client.post(URL, files=_file("two_column.pdf", PDF))
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == "2.0"
    assert body["overall_score"] == 80  # multi_column (high) only; sidebar suppressed
    assert body["content_score"] is not None
    assert body["file_format"] == "pdf"
    assert body["extractability"] == "full"
    assert body["roundtrip"] is None
    assert _checks(body)["multi_column"]["status"] == "fail"
    assert {profile["kind"] for profile in body["profiles"]} == {"heuristic"}
    assert [profile["id"] for profile in body["profiles"]] == [
        "workday", "taleo", "successfactors", "icims", "greenhouse", "lever"
    ]


async def test_same_upload_returns_byte_identical_body(client: AsyncClient) -> None:
    async with client:
        first = await client.post(URL, files=_file("swiss_single_like.pdf", PDF))
        second = await client.post(URL, files=_file("swiss_single_like.pdf", PDF))
    assert first.status_code == second.status_code == 200
    assert first.content == second.content


async def test_docx_and_explicit_content_language(client: AsyncClient) -> None:
    async with client:
        response = await client.post(
            URL, files=_file("clean.docx", DOCX), data={"content_language": "es"}
        )
    assert response.status_code == 200
    body = response.json()
    assert body["content_language"] == "es"
    assert _checks(body)["action_verbs"]["status"] == "not_applicable"


async def test_unsupported_content_language_is_rejected(client: AsyncClient) -> None:
    async with client:
        response = await client.post(
            URL, files=_file("clean.docx", DOCX), data={"content_language": "xx"}
        )
    assert response.status_code == 422


async def test_legacy_doc_reports_unsupported_format(client: AsyncClient) -> None:
    async with client:
        response = await client.post(URL, files=_file("legacy.doc", DOC))
    assert response.status_code == 200
    body = response.json()
    assert body["extractability"] == "unsupported_format"
    assert body["overall_score"] is None
    assert body["content_score"] is None


async def test_image_only_pdf_is_fatal(client: AsyncClient) -> None:
    async with client:
        response = await client.post(URL, files=_file("image_only.pdf", PDF))
    body = response.json()
    assert body["extractability"] == "none"
    assert body["overall_score"] <= 10


async def test_decompression_bomb_is_rejected_with_413(client: AsyncClient) -> None:
    async with client:
        response = await client.post(URL, files=_file("decompression_bomb.pdf", PDF))
    assert response.status_code == 413
    assert "too large" in response.json()["detail"]


async def test_invalid_pdf_bytes_return_422(client: AsyncClient) -> None:
    async with client:
        response = await client.post(URL, files={"file": ("r.pdf", b"%PDF-1.4 junk", PDF)})
    assert response.status_code == 422
    assert "Traceback" not in response.text


@pytest.mark.parametrize(
    ("name", "payload", "content_type", "status"),
    [
        ("resume.txt", b"hello", "text/plain", 400),
        ("resume.docx", b"%PDF-1.4", PDF, 400),
        ("resume.pdf", b"", PDF, 400),
        ("resume.pdf", b"x" * (MAX_FILE_SIZE + 1), PDF, 413),
    ],
)
async def test_upload_guards(
    client: AsyncClient, name: str, payload: bytes, content_type: str, status: int
) -> None:
    async with client:
        response = await client.post(URL, files={"file": (name, payload, content_type)})
    assert response.status_code == status


async def test_deadline_returns_504(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()

    def stuck(*args: object) -> None:
        release.wait(2)

    monkeypatch.setattr(engine, "PARSE_CHECK_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(engine, "check_document_sync", stuck)
    try:
        async with client:
            response = await client.post(URL, files=_file("clean.docx", DOCX))
        assert response.status_code == 504
    finally:
        release.set()
        await asyncio.sleep(0.05)


async def test_parse_check_persists_nothing(client: AsyncClient, isolated_db: Any) -> None:
    async with client:
        response = await client.post(URL, files=_file("clean_single_column.pdf", PDF))
        listing = await client.get("/api/v1/resumes/list")
    assert response.status_code == 200
    assert listing.status_code == 200
    assert listing.json()["data"] == []
