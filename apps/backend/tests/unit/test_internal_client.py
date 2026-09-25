"""Tests for the in-process API client (``app/internal_client.py``)."""

import logging

import pytest
from fastapi import FastAPI, File, Form, HTTPException, UploadFile

from app.internal_client import (
    GENERIC_SERVER_ERROR,
    InternalClient,
    InternalRequestError,
    InvalidIdentifierError,
    path_segment,
)


@pytest.mark.parametrize("value", ["", "..", "a/b", "a?b", "a#b", "a b", "x" * 129])
def test_unsafe_path_segments_are_rejected(value: str) -> None:
    with pytest.raises(InvalidIdentifierError):
        path_segment(value)


def test_safe_path_segment_is_returned() -> None:
    assert path_segment("3f2b-9c_1") == "3f2b-9c_1"


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/api/v1/items/{item_id}")
    async def item(item_id: str) -> dict[str, str]:
        if item_id == "missing":
            raise HTTPException(status_code=404, detail="Item not found")
        if item_id == "broken":
            raise RuntimeError("internal details")
        return {"id": item_id}

    return app


async def test_success_returns_the_route_response() -> None:
    client = InternalClient(_app())
    try:
        assert await client.get_json("/items/abc") == {"id": "abc"}
    finally:
        await client.aclose()


async def test_errors_carry_status_and_client_safe_detail() -> None:
    client = InternalClient(_app())
    try:
        with pytest.raises(InternalRequestError) as missing:
            await client.request("GET", "/items/missing")
        with pytest.raises(InternalRequestError) as broken:
            await client.request("GET", "/items/broken")
        with pytest.raises(InvalidIdentifierError):
            await client.request("GET", "/items/../config")
    finally:
        await client.aclose()
    assert (missing.value.status_code, str(missing.value)) == (404, "Item not found")
    assert (broken.value.status_code, str(broken.value)) == (500, GENERIC_SERVER_ERROR)


def _body_app() -> FastAPI:
    app = FastAPI()

    @app.post("/api/v1/echo")
    async def echo(payload: dict[str, int]) -> dict[str, int]:
        return payload

    @app.post("/api/v1/form")
    async def form(
        file: UploadFile = File(...), note: str = Form(...)
    ) -> dict[str, str | int]:
        return {"name": file.filename or "", "size": len(await file.read()), "note": note}

    @app.get("/api/v1/busy")
    async def busy() -> None:
        raise HTTPException(status_code=429, detail="Busy", headers={"Retry-After": "7"})

    return app


async def test_json_and_multipart_bodies_reach_the_route() -> None:
    client = InternalClient(_body_app())
    try:
        echoed = (await client.request("POST", "/echo", json={"a": 1})).json()
        uploaded = (
            await client.request(
                "POST",
                "/form",
                data={"note": "hi"},
                files={"file": ("cv.pdf", b"%PDF", "application/pdf")},
            )
        ).json()
    finally:
        await client.aclose()
    assert echoed == {"a": 1}
    assert uploaded == {"name": "cv.pdf", "size": 4, "note": "hi"}


async def test_validation_errors_report_fields_and_retry_after_is_kept() -> None:
    client = InternalClient(_body_app())
    try:
        with pytest.raises(InternalRequestError) as invalid:
            await client.request("POST", "/echo", json={"a": "not-a-number"})
        with pytest.raises(InternalRequestError) as busy:
            await client.request("GET", "/busy")
    finally:
        await client.aclose()
    assert invalid.value.status_code == 422
    assert str(invalid.value).startswith("Invalid request: a: ")
    assert "not-a-number" not in str(invalid.value)
    assert invalid.value.retry_after is None
    assert (busy.value.status_code, str(busy.value), busy.value.retry_after) == (429, "Busy", "7")


async def test_unhandled_exceptions_are_logged_to_the_callers_logger(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caller = logging.getLogger("tests.internal_client.caller")
    client = InternalClient(_app(), log=caller)
    try:
        with caplog.at_level(logging.ERROR, logger=caller.name):
            with pytest.raises(InternalRequestError):
                await client.request("GET", "/items/broken")
    finally:
        await client.aclose()
    [record] = [r for r in caplog.records if r.name == caller.name and r.exc_info]
    assert "GET /api/v1/items/broken" in record.getMessage()
