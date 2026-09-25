"""Backend selection and dialect dispatch, verified without a PostgreSQL server."""

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.schema import CreateIndex, CreateTable

from app.database import (
    POSTGRES_WRITER_LOCK_ARGS,
    Database,
    DatabaseBusyError,
    _translate_write_errors,
)
from app.db_engine import (
    _postgres_url_and_options,
    make_async_engine,
    make_sync_engine,
    normalize_database_url,
)
from app.models import Resume, TailoringPreview

PG_URL = "postgresql+psycopg://user:secret@db.example:5432/resume_matcher"


class _DriverError(Exception):
    """Stand-in for a psycopg error carrying a SQLSTATE."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


@pytest.mark.parametrize(
    "raw",
    [
        "postgres://user:secret@db.example:5432/resume_matcher",
        "postgresql://user:secret@db.example:5432/resume_matcher",
        PG_URL,
    ],
)
def test_postgres_urls_normalize_to_psycopg(raw: str) -> None:
    assert normalize_database_url(raw) == PG_URL


@pytest.mark.parametrize(
    "raw",
    ["sqlite:///data/resume_matcher.db", "postgresql+asyncpg://u:p@h/db", "mysql://u:p@h/db"],
)
def test_non_psycopg_urls_are_rejected(raw: str) -> None:
    with pytest.raises(ValueError, match="PostgreSQL"):
        normalize_database_url(raw)


def test_lock_timeout_is_prepended_and_caller_options_are_kept() -> None:
    url, connect_args = _postgres_url_and_options(
        f"{PG_URL}?options=-c%20search_path%3Dtenant"
    )
    assert "options" not in url.query
    assert connect_args == {"options": "-c lock_timeout=5s -c search_path=tenant"}
    _, defaults = _postgres_url_and_options(PG_URL)
    assert defaults == {"options": "-c lock_timeout=5s"}


def test_database_url_selects_postgres_without_touching_the_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path / "unused")
    database = Database(database_url="postgres://user:secret@db.example/resume_matcher")
    assert database.dialect == "postgresql"
    assert database.db_path is None
    assert database.database_url == make_url(
        "postgresql+psycopg://user:secret@db.example/resume_matcher"
    ).render_as_string(hide_password=False)
    assert not (tmp_path / "unused").exists()
    assert str(database._reserve_writer) == (
        f"SELECT pg_advisory_xact_lock({POSTGRES_WRITER_LOCK_ARGS})"
    )


def test_settings_database_url_is_the_default_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "database_url", None)
    default = Database()
    assert default.dialect == "sqlite"
    assert default.db_path == tmp_path / "resume_matcher.db"
    assert str(default._reserve_writer) == "BEGIN IMMEDIATE"

    monkeypatch.setattr(settings, "database_url", PG_URL)
    assert Database().dialect == "postgresql"
    # An explicit file path always means SQLite, whatever DATABASE_URL says.
    assert Database(db_path=tmp_path / "explicit.db").dialect == "sqlite"


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_database_url_setting_means_sqlite(blank: str) -> None:
    from app.config import Settings

    assert Settings(database_url=blank).database_url is None


def test_postgres_engines_use_read_committed_on_both_paths() -> None:
    pytest.importorskip("psycopg")
    sync_engine = make_sync_engine(PG_URL)
    async_engine = make_async_engine(PG_URL)
    try:
        assert sync_engine.dialect.name == "postgresql"
        assert sync_engine.dialect.driver == "psycopg"
        assert async_engine.dialect.driver == "psycopg"
        assert async_engine.dialect.is_async
        # Applied on every new connection; the live value is asserted with
        # SHOW transaction_isolation in tests/integration/test_postgres_backend.py.
        for dialect in (sync_engine.dialect, async_engine.sync_engine.dialect):
            assert dialect._on_connect_isolation_level == "READ COMMITTED"
    finally:
        sync_engine.dispose()


@pytest.mark.parametrize("sqlstate", ["55P03", "40001", "40P01"])
def test_postgres_contention_sqlstates_are_retryable(sqlstate: str) -> None:
    error = OperationalError("SELECT 1", {}, _DriverError(sqlstate))
    with pytest.raises(DatabaseBusyError) as caught:
        with _translate_write_errors():
            raise error
    assert caught.value.__cause__ is error


@pytest.mark.parametrize(
    ("error_type", "sqlstate"),
    [(IntegrityError, "23505"), (OperationalError, "57014"), (OperationalError, "08006")],
)
def test_other_postgres_errors_are_not_retryable(
    error_type: type[Any], sqlstate: str
) -> None:
    error = error_type("SELECT 1", {}, _DriverError(sqlstate))
    with pytest.raises(error_type) as caught:
        with _translate_write_errors():
            raise error
    assert caught.value is error


def test_postgres_ddl_has_single_master_predicate_and_byte_order_timestamps() -> None:
    index = next(
        index for index in Resume.__table__.indexes
        if index.name == "ux_resumes_single_master"
    )
    pg_index = str(CreateIndex(index).compile(dialect=postgresql.dialect()))
    assert pg_index.rstrip().endswith("WHERE is_master")
    pg_resumes = str(CreateTable(Resume.__table__).compile(dialect=postgresql.dialect()))
    assert 'created_at VARCHAR COLLATE "C"' in pg_resumes
    assert 'updated_at VARCHAR COLLATE "C"' in pg_resumes
    pg_previews = str(
        CreateTable(TailoringPreview.__table__).compile(dialect=postgresql.dialect())
    )
    assert 'expires_at VARCHAR COLLATE "C"' in pg_previews
    assert 'claim_expires_at VARCHAR COLLATE "C"' in pg_previews


def test_sqlite_ddl_is_unchanged() -> None:
    index = next(
        index for index in Resume.__table__.indexes
        if index.name == "ux_resumes_single_master"
    )
    assert str(CreateIndex(index).compile(dialect=sqlite.dialect())).rstrip().endswith(
        "WHERE is_master = 1"
    )
    ddl = str(CreateTable(Resume.__table__).compile(dialect=sqlite.dialect()))
    assert "COLLATE" not in ddl
    assert "created_at VARCHAR NOT NULL" in ddl
