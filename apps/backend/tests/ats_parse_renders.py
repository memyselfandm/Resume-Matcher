"""Committed real template renders for the own-output parse-check tests.

``tests/fixtures/ats_parse/renders/`` holds ``renders.tar.xz`` (the PDFs the
real download route produced, stored byte for byte), ``source.json`` (the
``processed_resume`` payload they rendered) and ``manifest.json`` (each PDF's
settings and the platform, Chromium version and image that rendered them).
``scripts/generate_ats_render_fixtures.py`` writes all three.
"""

import json
import tarfile
from functools import cache
from pathlib import Path
from typing import Any

RENDERS = Path(__file__).resolve().parent / "fixtures" / "ats_parse" / "renders"
ARCHIVE = RENDERS / "renders.tar.xz"


@cache
def _archive() -> dict[str, bytes]:
    renders: dict[str, bytes] = {}
    with tarfile.open(ARCHIVE, mode="r:xz") as archive:
        for member in archive.getmembers():
            extracted = archive.extractfile(member)
            if member.isfile() and extracted is not None:
                renders[member.name] = extracted.read()
    return renders


def render_pdf(stem: str) -> bytes:
    """Bytes of the committed render ``<stem>.pdf``."""
    return _archive()[f"{stem}.pdf"]


def render_stems() -> set[str]:
    return {name.removesuffix(".pdf") for name in _archive()}


@cache
def manifest() -> dict[str, Any]:
    return json.loads((RENDERS / "manifest.json").read_text())


def source() -> dict[str, Any]:
    """A fresh copy of the rendered ``processed_resume`` payload."""
    return json.loads((RENDERS / "source.json").read_text())
