"""Copy a Resume Matcher SQLite database into PostgreSQL, verified.

Every table is copied in foreign-key-safe order inside **one** PostgreSQL
transaction that also holds the application's writer lock for the target
schema, then verified before commit: per-table row counts and a per-row content
hash (canonical JSON of every column, keyed by primary key) must match the
source.
Any mismatch rolls the whole copy back.

API keys are copied as ciphertext, unchanged. They stay decryptable only with
the same ``data/.secret_key``, so keep ``DATA_DIR`` pointing at the directory
that holds it when the app runs against PostgreSQL.

A target that already holds rows is refused unless ``--force`` is given, in
which case its existing rows are deleted inside the same transaction.

Run with the ``postgres`` extra installed::

    uv run python -m app.scripts.migrate_sqlite_to_postgres \\
        --database-url postgresql+psycopg://user:pass@host:5432/resume_matcher
"""

import argparse
import hashlib
import json
import logging
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from sqlalchemy import Table, delete, func, select, text
from sqlalchemy.engine import Connection

from app.config import settings
from app.database import POSTGRES_WRITER_LOCK_ARGS
from app.db_engine import init_models_sync, make_sync_engine
from app.models import Base

logger = logging.getLogger(__name__)

_BATCH_SIZE = 500


class TargetNotEmptyError(RuntimeError):
    """The PostgreSQL target already holds rows and ``force`` was not given."""


class MigrationVerificationError(RuntimeError):
    """Copied rows do not match the source; the transaction was rolled back."""


def _tables() -> list[Table]:
    """All application tables, parents before children."""
    return list(Base.metadata.sorted_tables)


def _row_key(table: Table, row: Mapping[str, Any]) -> str:
    return json.dumps([row[column.name] for column in table.primary_key.columns])


def _row_hash(row: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        dict(row), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _content_hashes(connection: Connection, table: Table) -> dict[str, str]:
    """Map each row's primary key to the hash of all of its column values."""
    rows = connection.execute(select(table)).mappings()
    return {_row_key(table, row): _row_hash(row) for row in rows}


def _batches(rows: Iterable[Mapping[str, Any]]) -> Iterable[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    for row in rows:
        batch.append(dict(row))
        if len(batch) >= _BATCH_SIZE:
            yield batch
            batch = []
    if batch:
        yield batch


def migrate(sqlite_path: Path, database_url: str, *, force: bool = False) -> dict[str, int]:
    """Copy ``sqlite_path`` into ``database_url``; return per-table row counts.

    Raises:
        FileNotFoundError: The SQLite file does not exist.
        TargetNotEmptyError: The target has rows and ``force`` is false.
        MigrationVerificationError: Counts or content hashes differ.
    """
    if not sqlite_path.is_file():
        raise FileNotFoundError(f"SQLite database not found: {sqlite_path}")

    source = make_sync_engine(sqlite_path)
    target = make_sync_engine(database_url)
    try:
        # Bring an older SQLite file up to the current columns (the app's own
        # idempotent additive migration) and create the target tables.
        init_models_sync(source)
        init_models_sync(target)
        tables = _tables()
        with source.connect() as src, target.begin() as dst:
            dst.execute(text(f"SELECT pg_advisory_xact_lock({POSTGRES_WRITER_LOCK_ARGS})"))
            existing = {
                table.name: int(dst.scalar(select(func.count()).select_from(table)) or 0)
                for table in tables
            }
            occupied = {name: count for name, count in existing.items() if count}
            if occupied:
                if not force:
                    raise TargetNotEmptyError(
                        f"Target database is not empty ({occupied}); "
                        "rerun with --force to replace its rows"
                    )
                for table in reversed(tables):
                    dst.execute(delete(table))

            counts: dict[str, int] = {}
            for table in tables:
                copied = 0
                for batch in _batches(src.execute(select(table)).mappings()):
                    dst.execute(table.insert(), batch)
                    copied += len(batch)
                counts[table.name] = copied

            for table in tables:
                source_hashes = _content_hashes(src, table)
                target_hashes = _content_hashes(dst, table)
                if len(source_hashes) != len(target_hashes):
                    raise MigrationVerificationError(
                        f"{table.name}: {len(source_hashes)} source rows, "
                        f"{len(target_hashes)} copied"
                    )
                if source_hashes != target_hashes:
                    differing = sum(
                        1
                        for key, digest in source_hashes.items()
                        if target_hashes.get(key) != digest
                    )
                    raise MigrationVerificationError(
                        f"{table.name}: {differing} rows differ after copy"
                    )
        logger.info("SQLite → PostgreSQL copy verified: %s", counts)
        return counts
    finally:
        source.dispose()
        target.dispose()


def main(argv: list[str] | None = None) -> int:
    """Console entry point; returns the process exit status."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--sqlite",
        type=Path,
        default=settings.sqlite_path,
        help="Source SQLite file (default: DATA_DIR/resume_matcher.db)",
    )
    parser.add_argument(
        "--database-url",
        default=settings.database_url,
        help="Target PostgreSQL URL (default: DATABASE_URL)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace rows already present in the target",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    if not args.database_url:
        parser.error("--database-url or DATABASE_URL is required")
    try:
        counts = migrate(args.sqlite, args.database_url, force=args.force)
    except (
        FileNotFoundError,
        ValueError,
        TargetNotEmptyError,
        MigrationVerificationError,
    ) as error:
        logger.error("Migration aborted: %s", error)
        return 1
    print(json.dumps(counts, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
