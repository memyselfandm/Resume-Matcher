"""Tests for the in-process API client (``app/internal_client.py``)."""

import pytest
from fastapi import FastAPI, HTTPException

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
