"""Engine/session plumbing for the SQLAlchemy data layer.

Every ``Database`` instance owns its own engines (one async for the document
tables, one sync for the encrypted ``api_keys`` table read on the synchronous
LLM hot path) built from these factories. Keeping construction here lets tests
spin up fully isolated engines against a temp-file database.

SQLite (a file ``Path``) is the default backend. An optional PostgreSQL
backend is selected with a ``postgresql+psycopg://`` URL string; both engines
then use psycopg 3 under ``READ COMMITTED`` with a bounded ``lock_timeout``.
"""

from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import URL, Connection, Engine, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.models import Base

__all__ = [
    "Base",
    "POSTGRES_LOCK_TIMEOUT",
    "POSTGRES_WRITER_LOCK_ARGS",
    "POSTGRES_WRITER_LOCK_KEY",
    "make_async_engine",
    "make_sync_engine",
    "init_models_sync",
    "normalize_database_url",
]

# How long a PostgreSQL writer waits for the global write reservation (or any
# other lock) before failing with SQLSTATE 55P03. Mirrors SQLite busy_timeout.
POSTGRES_LOCK_TIMEOUT = "5s"

# Class key of the PostgreSQL transaction-scoped advisory lock that serializes
# every writer (async documents and sync api_keys alike), mirroring SQLite's
# single reserved writer. Any stable int4 works; this one spells "RMWR".
POSTGRES_WRITER_LOCK_KEY = 0x524D5752
# Advisory locks are database-wide, so the second key scopes the reservation to
# the schema the tables live in (``search_path``): deployments or test runs in
# different schemas of one database never serialize each other, while every
# writer on the same tables still does. ``coalesce`` keeps the lock taken even
# if no schema resolves: pg_advisory_xact_lock is strict and would silently
# return without locking on a NULL argument.
POSTGRES_WRITER_LOCK_ARGS = (
    f"{POSTGRES_WRITER_LOCK_KEY}, hashtext(coalesce(current_schema(), ''))"
)

_POSTGRES_SCHEMES = ("postgresql", "postgres")


def _apply_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
    """Set per-connection SQLite PRAGMAs.

    WAL improves concurrent read/write between the async (doc tables) and sync
    (api_keys) engines pointed at the same file; ``busy_timeout`` rides out the
    brief lock contention that creates; ``foreign_keys`` enforces relational
    integrity (off by default in SQLite).
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


def _url(path: Path, *, driver: str) -> str:
    """Build a SQLite URL. Absolute paths yield the required four slashes."""
    return f"sqlite+{driver}:///{path}" if driver else f"sqlite:///{path}"


def normalize_database_url(database_url: str) -> str:
    """Return a ``postgresql+psycopg`` URL for a PostgreSQL connection string.

    ``postgres://`` and ``postgresql://`` (as printed by most hosting
    providers) are rewritten to the psycopg 3 driver. Any other scheme or
    driver is rejected so a typo cannot silently fall back to SQLite.
    """
    url = make_url(database_url)
    backend, _, driver = url.drivername.partition("+")
    if backend not in _POSTGRES_SCHEMES or driver not in ("", "psycopg"):
        raise ValueError(
            "DATABASE_URL must be a PostgreSQL URL (postgresql+psycopg://...)"
        )
    return url.set(drivername="postgresql+psycopg").render_as_string(
        hide_password=False
    )


def _postgres_url_and_options(database_url: str) -> tuple[URL, dict[str, Any]]:
    """Split the URL from psycopg connect args, prepending the lock timeout.

    Caller-supplied ``options`` (for example ``-c search_path=...``) are kept
    and placed after the default so an explicit ``lock_timeout`` still wins.
    """
    url = make_url(normalize_database_url(database_url))
    extra = url.query.get("options")
    if isinstance(extra, tuple):
        extra = " ".join(extra)
    options = f"-c lock_timeout={POSTGRES_LOCK_TIMEOUT}"
    if extra:
        options = f"{options} {extra}"
    return url.difference_update_query(["options"]), {"options": options}


def _postgres_engine_kwargs(database_url: str) -> tuple[URL, dict[str, Any]]:
    url, connect_args = _postgres_url_and_options(database_url)
    return url, {
        "future": True,
        # Required for the global writer reservation: each statement after the
        # advisory lock must see rows committed by the previous writer. Under
        # REPEATABLE READ the snapshot would predate the lock.
        "isolation_level": "READ COMMITTED",
        "connect_args": connect_args,
        "pool_pre_ping": True,
    }


def make_async_engine(target: Path | str) -> AsyncEngine:
    """Create the async engine for the document tables.

    A ``Path`` selects SQLite (``aiosqlite``); a URL string selects
    PostgreSQL (psycopg 3 async).
    """
    if isinstance(target, str):
        url, kwargs = _postgres_engine_kwargs(target)
        return create_async_engine(url, **kwargs)
    engine = create_async_engine(_url(target, driver="aiosqlite"), future=True)
    event.listen(engine.sync_engine, "connect", _apply_sqlite_pragmas)
    return engine


def make_sync_engine(target: Path | str) -> Engine:
    """Create the sync engine used for the encrypted api_keys table.

    Key reads happen synchronously (``get_llm_config`` → ``load_config_file`` →
    ``resolve_api_key``), so a sync engine avoids threading async through
    ``llm.py``. It points at the same database as the async engine.
    """
    if isinstance(target, str):
        url, kwargs = _postgres_engine_kwargs(target)
        return create_engine(url, **kwargs)
    engine = create_engine(_url(target, driver=""), future=True)
    event.listen(engine, "connect", _apply_sqlite_pragmas)
    return engine


def _postgres_upgrade_pending(conn: Connection) -> bool:
    """Whether the additive upgrade in ``init_models_sync`` has work to do."""
    inspector = inspect(conn)
    resume_columns = {column["name"] for column in inspector.get_columns("resumes")}
    preview_columns = {
        column["name"] for column in inspector.get_columns("tailoring_previews")
    }
    resume_indexes = {index["name"] for index in inspector.get_indexes("resumes")}
    if {"interview_prep", "processing_token", "is_default_master"} - resume_columns:
        return True
    if {"improvements", "source_data"} - preview_columns:
        return True
    if (
        "ux_resumes_single_master" in resume_indexes
        or "ux_resumes_single_default_master" not in resume_indexes
    ):
        return True
    return bool(
        conn.exec_driver_sql(
            "SELECT EXISTS (SELECT 1 FROM resumes WHERE is_master) "
            "AND NOT EXISTS (SELECT 1 FROM resumes WHERE is_default_master)"
        ).scalar()
    )


def init_models_sync(engine: Engine) -> None:
    """Create all tables (idempotent) using a sync engine connection."""
    Base.metadata.create_all(engine)

    # ``create_all`` does not ALTER existing tables. Keep this additive
    # migration idempotent so older local databases can load resumes safely.
    with engine.begin() as conn:
        postgres = conn.dialect.name == "postgresql"
        if postgres and _postgres_upgrade_pending(conn):
            # The upgrade can promote a default master, so it takes the same
            # writer reservation as every other write, and a concurrently
            # starting process re-inspects only after this one has finished.
            # An up-to-date schema skips the lock, so opening a Database never
            # waits on a busy writer.
            conn.execute(text(f"SELECT pg_advisory_xact_lock({POSTGRES_WRITER_LOCK_ARGS})"))
        inspector = inspect(conn)
        resume_columns = {column["name"] for column in inspector.get_columns("resumes")}
        if "interview_prep" not in resume_columns:
            conn.exec_driver_sql("ALTER TABLE resumes ADD COLUMN interview_prep TEXT")
        if "processing_token" not in resume_columns:
            conn.exec_driver_sql("ALTER TABLE resumes ADD COLUMN processing_token TEXT")

        # SQLite stores booleans as 0/1; PostgreSQL has a real boolean type.
        true, false = ("TRUE", "FALSE") if postgres else ("1", "0")
        # Same predicate as the model's partial index for each dialect.
        default_predicate = "is_default_master" if postgres else "is_default_master = 1"
        if "is_master" in resume_columns:
            if "is_default_master" not in resume_columns:
                conn.exec_driver_sql(
                    f"ALTER TABLE resumes ADD COLUMN is_default_master BOOLEAN NOT NULL DEFAULT {false}"
                )
            # Multi-track masters: the single-master slot is replaced by a
            # single-default slot. create_all never drops indexes on existing tables.
            conn.exec_driver_sql("DROP INDEX IF EXISTS ux_resumes_single_master")
            if "created_at" in resume_columns:
                conn.exec_driver_sql(
                    f"UPDATE resumes SET is_default_master = {true} WHERE resume_id = ("
                    f"SELECT resume_id FROM resumes WHERE is_master = {true} "
                    "ORDER BY created_at, resume_id LIMIT 1) "
                    f"AND NOT EXISTS (SELECT 1 FROM resumes WHERE is_default_master = {true})"
                )
            conn.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_resumes_single_default_master "
                f"ON resumes (is_default_master) WHERE {default_predicate}"
            )

        preview_columns = {
            column["name"] for column in inspector.get_columns("tailoring_previews")
        }
        if "improvements" not in preview_columns:
            conn.exec_driver_sql("ALTER TABLE tailoring_previews ADD COLUMN improvements JSON")
        if "source_data" not in preview_columns:
            conn.exec_driver_sql("ALTER TABLE tailoring_previews ADD COLUMN source_data JSON")
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_preview_compatibility ON tailoring_previews (source_id, job_id, payload_hash, created_at)")
