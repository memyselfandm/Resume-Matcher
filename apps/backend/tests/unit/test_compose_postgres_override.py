"""The opt-in PostgreSQL compose override stays in step with the backend."""

import re
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]


def test_override_installs_exactly_the_pinned_postgres_extra() -> None:
    pyproject = tomllib.loads(
        (_REPO_ROOT / "apps" / "backend" / "pyproject.toml").read_text()
    )
    extra = pyproject["project"]["optional-dependencies"]["postgres"]
    override = (_REPO_ROOT / "docker-compose.postgres.yml").read_text()
    installs = re.findall(r'pip install --no-cache-dir "([^"]+)"', override)
    assert installs == extra


def test_override_never_retags_the_published_image() -> None:
    override = (_REPO_ROOT / "docker-compose.postgres.yml").read_text()
    images = re.findall(r"^\s*image:\s*(\S+)", override, flags=re.MULTILINE)
    assert images == ["resume-matcher-postgres:local"]
    assert "FROM ghcr.io/srbhr/resume-matcher:latest" in override
    assert "additional_contexts" not in override
