"""Stable identity for the data directory backing this process.

The MCP stdio server and the HTTP backend that serves the print page must read
the same database for PDF export to render the resume the agent just edited.
Each data directory carries a random UUID in ``instance_id``; comparing the
value reported by ``GET /api/v1/health`` with the local one proves (or
disproves) that both processes share storage.
"""

import logging
import os
from pathlib import Path
from uuid import UUID, uuid4

from app.config import settings

logger = logging.getLogger(__name__)

INSTANCE_ID_FILENAME = "instance_id"


def _read_instance_id(path: Path) -> str | None:
    """Return the stored UUID, or None when the file is missing or invalid."""
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    try:
        return str(UUID(value))
    except ValueError:
        logger.warning("Ignoring invalid database instance id in %s", path)
        return None


def get_db_instance_id(data_dir: Path | None = None) -> str:
    """Return the data directory's instance UUID, creating it on first use.

    Creation uses an exclusive open so concurrent first callers (backend and
    MCP process starting together) converge on a single value. An unreadable
    or corrupt file is replaced atomically.
    """
    directory = data_dir if data_dir is not None else settings.data_dir
    path = directory / INSTANCE_ID_FILENAME

    existing = _read_instance_id(path)
    if existing is not None:
        return existing

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
