"""Stable identity for the data directory backing this process.

The MCP stdio server and the HTTP backend that serves the print page must read
the same database for PDF export to render the resume the agent just edited.
Each data directory with an established database carries a random UUID in
``instance_id``; comparing the value reported by ``GET /api/v1/health`` with
the local one proves (or disproves) that both processes share storage.
"""

import logging
import os
import time
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from app.config import settings

logger = logging.getLogger(__name__)

INSTANCE_ID_FILENAME = "instance_id"
# Reads that find an empty file retry briefly before treating it as corrupt:
# a writer using the non-atomic fallback may be between create and write.
_EMPTY_READ_RETRIES = 3
_EMPTY_READ_DELAY_SECONDS = 0.01
_MAX_ATTEMPTS = 5

ReadState = Literal["valid", "missing", "empty", "invalid"]

# Ids are immutable once on disk, so each directory is read once. Only values
# read back from disk are cached, never a value this process merely proposed.
_cache: dict[Path, str] = {}


def _directory(data_dir: Path | None) -> Path:
    return data_dir if data_dir is not None else settings.data_dir


def database_established(data_dir: Path | None = None) -> bool:
    """Return whether the data directory already holds the SQLite database."""
    return (_directory(data_dir) / settings.sqlite_path.name).exists()


def _read_instance_id(path: Path) -> tuple[ReadState, str | None]:
    """Read the id file and classify what was found."""
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return "missing", None
    except ValueError:  # includes UnicodeDecodeError
        logger.warning("Ignoring undecodable database instance id in %s", path)
        return "invalid", None
    if not value:
        return "empty", None
    try:
        return "valid", str(UUID(value))
    except ValueError:
        logger.warning("Ignoring invalid database instance id in %s", path)
        return "invalid", None


def _temp_file_with_new_id(directory: Path) -> Path:
    """Write a fresh id to a private temp file in ``directory``."""
    new_id = str(uuid4())
    temp_path = directory / f".{INSTANCE_ID_FILENAME}.{new_id}.tmp"
    temp_path.write_text(new_id, encoding="utf-8")
    return temp_path


def _create_if_absent(directory: Path, path: Path) -> None:
    """Atomically publish a new id unless another writer already did.

    ``os.link`` creates the target with its full content in one step and
    fails if it exists, so readers never observe a partial file.
    """
    temp_path = _temp_file_with_new_id(directory)
    try:
        os.link(temp_path, path)
    except FileExistsError:
        pass
    except OSError:
        # Filesystems without hard links: exclusive create, then write.
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(temp_path.read_text(encoding="utf-8"))
    finally:
        temp_path.unlink(missing_ok=True)


def _replace_corrupt(directory: Path, path: Path) -> None:
    """Atomically replace an unusable id file with a fresh id."""
    os.replace(_temp_file_with_new_id(directory), path)


def get_db_instance_id(data_dir: Path | None = None, *, create: bool = True) -> str | None:
    """Return the data directory's instance UUID.

    With ``create`` (the default) a missing or corrupt id is (re)created and
    the value on disk is returned, so concurrent creators converge on one id.
    Without it, only an existing valid id is returned and None means "not
    established yet".
    """
    directory = _directory(data_dir)
    cached = _cache.get(directory)
    if cached is not None:
        return cached

    path = directory / INSTANCE_ID_FILENAME
    empty_reads = 0
    for _ in range(_MAX_ATTEMPTS + _EMPTY_READ_RETRIES):
        state, value = _read_instance_id(path)
        if state == "valid" and value is not None:
            _cache[directory] = value
            return value
        if not create:
            return None
        if state == "missing":
            directory.mkdir(parents=True, exist_ok=True)
            _create_if_absent(directory, path)
        elif state == "empty" and empty_reads < _EMPTY_READ_RETRIES:
            empty_reads += 1
            time.sleep(_EMPTY_READ_DELAY_SECONDS)
        else:
            _replace_corrupt(directory, path)
    raise OSError(f"Could not establish a database instance id in {directory}")
