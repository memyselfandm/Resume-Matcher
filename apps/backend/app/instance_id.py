"""Stable identity for the data directory backing this process.

The MCP stdio server and the HTTP backend that serves the print page must read
the same database for PDF export to render the resume the agent just edited.
Each data directory with an established database carries a random UUID in
``instance_id``; comparing the value reported by ``GET /api/v1/health`` with
the local one proves (or disproves) that both processes share storage.
"""

import logging
import os
from pathlib import Path
from uuid import UUID, uuid4

from app.config import settings

logger = logging.getLogger(__name__)

INSTANCE_ID_FILENAME = "instance_id"

# Ids are immutable once written, so each directory is read from disk once.
_cache: dict[Path, str] = {}


def _directory(data_dir: Path | None) -> Path:
    return data_dir if data_dir is not None else settings.data_dir


def database_established(data_dir: Path | None = None) -> bool:
    """Return whether the data directory already holds the SQLite database."""
    return (_directory(data_dir) / settings.sqlite_path.name).exists()


def _read_instance_id(path: Path) -> str | None:
    """Return the stored UUID, or None when the file is missing or invalid."""
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except ValueError:  # includes UnicodeDecodeError
        logger.warning("Ignoring undecodable database instance id in %s", path)
        return None
    try:
        return str(UUID(value))
    except ValueError:
        logger.warning("Ignoring invalid database instance id in %s", path)
        return None


def _write_new_instance_id(directory: Path, path: Path) -> str:
    """Create the id file, converging with a concurrent creator if any."""
    directory.mkdir(parents=True, exist_ok=True)
    new_id = str(uuid4())
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        concurrent = _read_instance_id(path)
        if concurrent is not None:
            return concurrent
        # The existing file is corrupt: replace it atomically.
        temp_path = directory / f".{INSTANCE_ID_FILENAME}.{new_id}.tmp"
        temp_path.write_text(new_id, encoding="utf-8")
        os.replace(temp_path, path)
        return new_id
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(new_id)
    return new_id


def get_db_instance_id(data_dir: Path | None = None, *, create: bool = True) -> str | None:
    """Return the data directory's instance UUID.

    With ``create`` (the default) a missing or corrupt id is (re)created, so a
    string is always returned. Without it, only an existing valid id is
    returned and None means "not established yet".
    """
    directory = _directory(data_dir)
    cached = _cache.get(directory)
    if cached is not None:
        return cached

    path = directory / INSTANCE_ID_FILENAME
    instance_id = _read_instance_id(path)
    if instance_id is None and create:
        instance_id = _write_new_instance_id(directory, path)
    if instance_id is not None:
        _cache[directory] = instance_id
    return instance_id
