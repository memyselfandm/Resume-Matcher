"""Resource-limit tests for the parse-check engine (hostile but tiny PDFs).

Dense single-page PDFs used to reach pdfminer's quadratic text-box grouping:
a 10 KB page with 4,000 boxes took ~50 s and >3 GB. These tests pin the
per-page, per-document, line, and deadline bounds that replaced it.
"""

import asyncio
import io
import logging
import threading
import time
import tracemalloc
import zlib

import anyio
import pytest

from app.services import parser
from app.services.ats_parse import check_document_sync, engine
from app.services.ats_parse import extract as extract_module
from app.services.ats_parse.extract import (
    MAX_LINES_PER_PAGE,
    PageLayout,
    TextLine,
    extract_document,
)
from app.services.ats_parse.layout_checks import find_gutters


def _dense_pdf(boxes: int, pages: int = 1) -> bytes:
    """One Tj per box on a grid, so every box is its own text object."""
    operations = [
        f"BT /F1 3 Tf {10 + (i % 120) * 5} {10 + (i // 120) * 7.5:.1f} Td (w) Tj ET"
        for i in range(boxes)
    ]
    content = zlib.compress("\n".join(operations).encode())
    kids = " ".join(f"{3 + 2 * index} 0 R" for index in range(pages))
    font = 3 + 2 * pages
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode(),
    ]
    for index in range(pages):
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {4 + 2 * index} 0 R >>".encode()
        )
        objects.append(
            f"<< /Length {len(content)} /Filter /FlateDecode >>\nstream\n".encode()
            + content
            + b"\nendstream"
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    output = io.BytesIO()
    output.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(output.tell())
        output.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = output.tell()
    output.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        output.write(f"{offset:010d} 00000 n \n".encode())
    output.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return output.getvalue()


def _truncated_params(pdf: bytes) -> dict[str, object]:
    report = check_document_sync(pdf, "dense.pdf")
    return next(check.params for check in report.checks if check.id == "truncated")


@pytest.mark.parametrize("boxes", [1_000, 4_000])
def test_dense_single_page_completes_fast_with_bounded_memory(boxes: int) -> None:
    pdf = _dense_pdf(boxes)
    assert len(pdf) < 15_000
    tracemalloc.start()
    started = time.perf_counter()
    try:
        check_document_sync(pdf, "dense.pdf")
        elapsed = time.perf_counter() - started
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert elapsed < 5.0
    assert peak < 150 * 1024 * 1024


def test_page_over_char_cap_is_skipped_before_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(extract_module, "MAX_PAGE_CHARS", 500)
    params = _truncated_params(_dense_pdf(600, pages=2))
    assert params["dense_pages"] == [1, 2]


def test_document_budget_stops_interpreting_later_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(extract_module, "MAX_DOCUMENT_CHARS", 1_000)
    document = extract_document(_dense_pdf(600, pages=4), "dense.pdf")
    assert document.truncated_chars is True
    assert document.total_pages == 4
    assert len(document.pages) == 1


def test_pages_over_line_cap_keep_text_but_skip_geometry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(extract_module, "MAX_LINES_PER_PAGE", 20)
    document = extract_document(_dense_pdf(4_000), "dense.pdf")
    assert document.dense_pages == (1,)
    assert document.pages[0].lines == ()
    assert document.text


def test_expired_deadline_stops_extraction() -> None:
    with pytest.raises(TimeoutError):
        extract_document(_dense_pdf(1_000), "dense.pdf", deadline=time.monotonic() - 1)


def test_expired_deadline_stops_gutter_scan() -> None:
    lines = tuple(
        TextLine(30.0 + 300.0 * (i % 2), 700.0 - (i // 2) * 11.0, 200.0 + 300.0 * (i % 2),
                 709.0 - (i // 2) * 11.0, "w")
        for i in range(40)
    )
    page = PageLayout(1, 612.0, 792.0, lines, (), 400)
    with pytest.raises(TimeoutError):
        find_gutters(page, deadline=time.monotonic() - 1)


def test_gutter_scan_at_line_cap_is_fast() -> None:
    lines = tuple(
        TextLine(20.0 + column * 95.0, 780.0 - row * 7.6 - 6.0,
                 20.0 + column * 95.0 + 40.0 + (row % 7) * 5.0, 780.0 - row * 7.6, "w")
        for column in range(6)
        for row in range(MAX_LINES_PER_PAGE // 6)
    )
    started = time.perf_counter()
    find_gutters(PageLayout(1, 612.0, 792.0, lines, (), 6_000))
    assert time.perf_counter() - started < 2.0


async def test_parse_checks_use_their_own_single_slot_limiter(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A stuck parse check never blocks upload conversion, and checks queue."""
    entered = threading.Event()
    release = threading.Event()
    calls: list[object] = []

    def stuck(*args: object) -> None:
        calls.append(args)
        entered.set()
        release.wait(5)
        raise RuntimeError("synthetic failure after release")

    def convert(content: bytes, filename: str) -> str:
        return "converted"

    monkeypatch.setattr(engine, "check_document_sync", stuck)
    monkeypatch.setattr(parser, "_parse_document_sync", convert)
    # One upload slot: sharing it with the stuck parse check would block uploads.
    monkeypatch.setattr(parser, "_DOCUMENT_CONVERSION_LIMITER", anyio.CapacityLimiter(1))
    monkeypatch.setattr(engine, "PARSE_CHECK_TIMEOUT_SECONDS", 0.2)
    first = asyncio.create_task(engine.run_parse_check(b"x", "a.pdf"))
    assert await asyncio.to_thread(entered.wait, 2)
    try:
        # Uploads still convert while the parse-check slot is held.
        assert await asyncio.wait_for(parser.parse_document(b"x", "r.pdf"), 1) == "converted"
        # A second parse check cannot be admitted and times out in the queue.
        with pytest.raises(TimeoutError):
            await engine.run_parse_check(b"x", "b.pdf")
        assert len(calls) == 1
        with pytest.raises(TimeoutError):
            await first
    finally:
        with caplog.at_level(logging.ERROR, logger="app.services.parser"):
            release.set()
            for _ in range(50):
                if "ATS parse check failed after request cancellation" in caplog.text:
                    break
                await asyncio.sleep(0.02)
    assert "ATS parse check failed after request cancellation" in caplog.text
