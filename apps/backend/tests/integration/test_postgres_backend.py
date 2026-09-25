"""PostgreSQL backend behavior that SQLite-driver tests cannot exercise.

Opt-in: runs only with ``TEST_DATABASE_URL=postgresql+psycopg://...`` (see
docs/agent/architecture/storage-transactions.md). Each test gets its own schema
through ``isolated_db``. These are the PostgreSQL counterparts of the
``sqlite_only`` tests plus the invariants the plan requires on PostgreSQL:
single master under concurrency, lock timeout → 503, distinct tracker
positions, byte-order timestamp ordering and a verified migration round trip.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import Engine, event, func, inspect, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapper, Session

from app import crypto
from app.database import POSTGRES_WRITER_LOCK_ARGS, Database, DatabaseBusyError
from app.db_engine import init_models_sync, make_sync_engine
from app.main import app
from app.models import Application, Base, Job, Resume, TailoringPreview
from app.scripts import migrate_sqlite_to_postgres as pg_migration
from tests.integration.test_manual_application_transactions import MANUAL_CARD
from tests.integration.test_storage_busy_writes import fast_busy_database  # noqa: F401

pytestmark = pytest.mark.postgres_only


@asynccontextmanager
async def hold_writer(database: Database) -> AsyncIterator[None]:
    """Hold the app's global writer reservation on a separate connection."""
    async with database._session() as writer:
        await writer.execute(database._reserve_writer)
        yield


@contextmanager
def side_engine(database: Database) -> Iterator[Engine]:
    """A separate sync engine on the same schema, for observers/contenders."""
    assert database.database_url is not None
    engine = make_sync_engine(database.database_url)
    try:
        yield engine
    finally:
        engine.dispose()


def _client() -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


# -- engine configuration ----------------------------------------------------


async def test_both_engines_run_read_committed_with_bounded_lock_wait(
    isolated_db: Database,
) -> None:
    async with isolated_db._session() as session:
        assert (await session.scalar(text("SHOW transaction_isolation"))) == "read committed"
        assert (await session.scalar(text("SHOW lock_timeout"))) == "5s"
        assert (await session.scalar(text("SELECT current_schema()"))).startswith("test_")
    with isolated_db._sync() as session:
        assert session.scalar(text("SHOW transaction_isolation")) == "read committed"
        assert session.scalar(text("SHOW lock_timeout")) == "5s"
        assert session.scalar(text("SELECT current_schema()")).startswith("test_")


async def test_harness_bounds_every_test_connection(isolated_db: Database) -> None:
    """The conftest hang guards reach both app engines without displacing
    the app's own lock_timeout, so a stalled server fails the run."""
    async with isolated_db._session() as session:
        raw = await session.connection()
        params = (await raw.get_raw_connection()).driver_connection.info.get_parameters()
        assert params["connect_timeout"] == "10"
        assert params["keepalives_idle"] == "10"
        assert (await session.scalar(text("SHOW statement_timeout"))) == "1min"
        assert (
            await session.scalar(text("SHOW idle_in_transaction_session_timeout"))
        ) == "1min"
    with isolated_db._sync() as session:
        assert session.scalar(text("SHOW statement_timeout")) == "1min"
        assert session.scalar(text("SHOW lock_timeout")) == "5s"


def test_additive_migration_is_idempotent_on_postgres(isolated_db: Database) -> None:
    """PostgreSQL counterpart of the sqlite_only PRAGMA table_info migrations."""
    with side_engine(isolated_db) as engine:
        Base.metadata.drop_all(engine)  # start from the pre-migration layout
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE resumes (resume_id TEXT PRIMARY KEY, "
                "content TEXT NOT NULL, content_type TEXT DEFAULT 'md')"
            )
            connection.exec_driver_sql(
                "CREATE TABLE tailoring_previews (preview_id TEXT PRIMARY KEY, "
                "source_id TEXT, job_id TEXT, payload_hash TEXT, source_hash TEXT, "
                "job_hash TEXT, created_at TEXT, expires_at TEXT, "
                "result_resume_id TEXT, claim_token TEXT, claim_expires_at TEXT, "
                "response_data JSON)"
            )
        init_models_sync(engine)
        init_models_sync(engine)
        inspector = inspect(engine)
        resume_columns = [column["name"] for column in inspector.get_columns("resumes")]
        preview_columns = [
            column["name"] for column in inspector.get_columns("tailoring_previews")
        ]
        indexes = {index["name"] for index in inspector.get_indexes("tailoring_previews")}
    assert resume_columns.count("interview_prep") == 1
    assert resume_columns.count("processing_token") == 1
    assert preview_columns.count("improvements") == 1
    assert "ix_preview_compatibility" in indexes


# -- writer reservation / contention -----------------------------------------


async def test_concurrent_master_replacement_leaves_exactly_one_master(
    isolated_db: Database,
) -> None:
    await isolated_db.create_resume(
        content="Stuck master", is_master=True, processing_status="failed"
    )
    other = Database(database_url=isolated_db.database_url)
    try:
        uploads = [
            (isolated_db if index % 2 else other).create_resume_atomic_master(
                content=f"Upload {index}", processing_status="processing"
            )
            for index in range(8)
        ]
        # Without the writer advisory lock two READ COMMITTED writers both see
        # the failed master and the second insert violates the partial index.
        created = await asyncio.gather(*uploads)
        assert len(created) == 8

        resumes = await isolated_db.list_resumes()
        promotions = [
            (isolated_db if index % 2 else other).set_master_resume(row["resume_id"])
            for index, row in enumerate(resumes)
        ]
        assert all(await asyncio.gather(*promotions))
    finally:
        await other.close()

    async with isolated_db._session() as session:
        masters = await session.scalar(
            select(func.count()).select_from(Resume).where(Resume.is_master.is_(True))
        )
    assert masters == 1


async def test_writer_lock_is_scoped_to_the_schema(
    isolated_db: Database, second_postgres_schema_url: str
) -> None:
    """Advisory locks are database-wide; the writer key must not be."""
    neighbour = Database(database_url=second_postgres_schema_url)
    same_schema = Database(database_url=isolated_db.database_url)
    try:
        async with hold_writer(isolated_db):
            # Another schema in the same database holds its own writer at the
            # same time and commits (a 5s lock wait would fail this).
            async with hold_writer(neighbour):
                pass
            await neighbour.create_resume(content="Neighbour")
            # The same schema is still serialized.
            async with same_schema._session() as session:
                await session.execute(text("SET LOCAL lock_timeout = '50ms'"))
                with pytest.raises(DBAPIError) as caught:
                    await session.execute(same_schema._reserve_writer)
            assert getattr(caught.value.orig, "sqlstate", None) == "55P03"
        assert await isolated_db.list_resumes() == []
        assert [row["content"] for row in await neighbour.list_resumes()] == ["Neighbour"]
    finally:
        await neighbour.close()
        await same_schema.close()


async def test_partial_unique_index_rejects_a_second_master(isolated_db: Database) -> None:
    await isolated_db.create_resume(content="Master", is_master=True)
    with pytest.raises(IntegrityError):
        await isolated_db.create_resume(content="Second master", is_master=True)
    # Non-master rows are unconstrained by the partial index.
    await isolated_db.create_resume(content="Tailored A")
    await isolated_db.create_resume(content="Tailored B")
    assert len(await isolated_db.list_resumes()) == 3


async def test_lock_timeout_is_database_busy_and_http_503(
    fast_busy_database: Database,
) -> None:
    database = fast_busy_database
    row = await database.create_resume(content="Synthetic", title="Original")
    async with hold_writer(database):
        with pytest.raises(DatabaseBusyError) as busy:
            await database.update_resume(row["resume_id"], {"title": "Changed"})
        cause = busy.value.__cause__
        assert isinstance(cause, DBAPIError)
        assert getattr(cause.orig, "sqlstate", None) == "55P03"
        with pytest.raises(DatabaseBusyError):
            database.set_api_key_ciphertext("openai", "ciphertext")
        async with _client() as client:
            response = await client.post(
                "/api/v1/jobs/upload", json={"job_descriptions": ["Synthetic job"]}
            )
            # Readers never take the writer reservation.
            assert (await client.get("/api/v1/resumes/list")).status_code == 200
    assert response.status_code == 503, response.text
    assert response.headers["retry-after"] == "1"
    assert response.json() == {"detail": "Database is busy. Please retry shortly."}
    assert (await database.get_stats())["total_jobs"] == 0
    stored = await database.get_resume(row["resume_id"])
    assert stored is not None and stored["title"] == "Original"


async def test_default_lock_timeout_bounds_a_blocked_sync_key_write(
    isolated_db: Database,
) -> None:
    """The connect-time ``lock_timeout=5s`` (no test override) ends the wait."""
    async with hold_writer(isolated_db):
        started = time.monotonic()
        with pytest.raises(DatabaseBusyError):
            await asyncio.to_thread(
                isolated_db.set_api_key_ciphertext, "openai", "ciphertext"
            )
        waited = time.monotonic() - started
    assert 4.5 <= waited < 15
    assert isolated_db.get_api_key_ciphertexts() == {}


async def test_non_busy_postgres_write_error_is_not_retryable(
    isolated_db: Database,
) -> None:
    with pytest.raises(DBAPIError) as caught:
        async with isolated_db._write_session() as session:
            await session.execute(text("INSERT INTO synthetic_missing_table VALUES (1)"))
    assert not isinstance(caught.value, DatabaseBusyError)
    assert getattr(caught.value.orig, "sqlstate", None) == "42P01"


async def test_busy_processing_claim_and_finish_preserve_the_owner(
    fast_busy_database: Database,
) -> None:
    """Counterpart of the sqlite_only processing busy-retirement tests."""
    database = fast_busy_database
    row = await database.create_resume(content="Synthetic", processing_status="failed")
    token = await database.claim_resume_processing(row["resume_id"])
    assert token is not None
    async with hold_writer(database):
        async with _client() as client:
            response = await client.post(
                f"/api/v1/resumes/{row['resume_id']}/retry-processing"
            )
        with pytest.raises(DatabaseBusyError):
            await database.finish_resume_processing(
                row["resume_id"], token, processing_status="ready", processed_data={}
            )
    assert response.status_code == 503
    stored = await database.get_resume(row["resume_id"])
    assert stored is not None and stored["processing_status"] == "processing"
    assert await database.finish_resume_processing(
        row["resume_id"],
        token,
        processing_status="ready",
        processed_data={"summary": "original owner"},
    ) == "committed"


async def test_concurrent_tracker_creates_get_distinct_positions(
    isolated_db: Database,
) -> None:
    other = Database(database_url=isolated_db.database_url)
    try:
        cards = await asyncio.gather(*[
            (isolated_db if index % 2 else other).create_application(
                job_id=f"job-{index}", resume_id=f"resume-{index}", status="applied"
            )
            for index in range(12)
        ])
        duplicates = await asyncio.gather(*[
            (isolated_db if index % 2 else other).create_application(
                job_id="job-same", resume_id="resume-same", status="saved"
            )
            for index in range(5)
        ])
    finally:
        await other.close()
    assert sorted(card["position"] for card in cards) == list(range(12))
    assert len({card["application_id"] for card in duplicates}) == 1
    assert len(await isolated_db.list_applications(status="saved")) == 1


# -- manual tracker cards (counterparts of sqlite3-observer tests) -----------


async def test_manual_card_commit_exposes_job_and_card_together(
    isolated_db: Database,
) -> None:
    observed: list[tuple[int, int]] = []
    with side_engine(isolated_db) as engine:

        def observe_after_commit(session: Session) -> None:
            del session
            with engine.connect() as observer:
                jobs = observer.scalar(select(func.count()).select_from(Job))
                cards = observer.scalar(select(func.count()).select_from(Application))
            observed.append((int(jobs or 0), int(cards or 0)))

        event.listen(Session, "after_commit", observe_after_commit)
        try:
            async with _client() as client:
                response = await client.post("/api/v1/applications", json=MANUAL_CARD)
        finally:
            event.remove(Session, "after_commit", observe_after_commit)
    assert response.status_code == 200, response.text
    assert observed == [(1, 1)]


async def test_failed_manual_card_rolls_back_job_under_later_contention(
    fast_busy_database: Database,
) -> None:
    database = fast_busy_database
    contended = False
    with side_engine(database) as engine:
        contender = engine.connect()

        def reject_card(
            mapper: Mapper[Any], connection: Connection, target: Application
        ) -> None:
            del mapper, connection, target
            raise RuntimeError("Synthetic card insert failure")

        def contend_after_rollback(session: Session) -> None:
            nonlocal contended
            del session
            if not contended:
                contender.execute(
                    text(f"SELECT pg_advisory_lock({POSTGRES_WRITER_LOCK_ARGS})")
                )
                contended = True

        event.listen(Application, "before_insert", reject_card)
        event.listen(Session, "after_rollback", contend_after_rollback)
        try:
            async with _client() as client:
                response = await client.post("/api/v1/applications", json=MANUAL_CARD)
        finally:
            event.remove(Application, "before_insert", reject_card)
            event.remove(Session, "after_rollback", contend_after_rollback)
            contender.execute(
                text(f"SELECT pg_advisory_unlock({POSTGRES_WRITER_LOCK_ARGS})")
            )
            contender.close()
    assert contended
    assert response.status_code == 500
    assert "Synthetic card insert failure" not in response.text
    assert (await database.get_stats())["total_jobs"] == 0
    assert await database.list_applications() == []


# -- timestamp collation ------------------------------------------------------

# App-written values are UTC ``isoformat()`` strings; zero-microsecond values
# omit ".ffffff". The last two are client-style ISO strings (``applied_at`` is
# user-supplied) whose relative order differs between glibc's en_US collation,
# which ignores punctuation, and byte order. They make the guard below
# meaningful: without ``COLLATE "C"`` this ordering would not match ``sorted()``.
_TIMESTAMPS = [
    "2026-01-01T00:00:00.500000+00:00",
    "2026-01-01T00:00:00+00:00",
    "2025-12-31T23:59:59.999999+00:00",
    "2026-01-01T00:00:01+00:00",
    "2026-01-01T00:00:00.000001+00:00",
    "2026-01-01T00:00:00.999999+00:00",
    "2026-01-01T00:00:00.000001Z",
    "2026-01-01T00:00:00-05:00",
]


async def test_iso_timestamps_order_by_bytes_under_a_non_c_database_collation(
    isolated_db: Database,
) -> None:
    async with isolated_db._session() as session:
        collation = await session.scalar(
            text("SELECT datcollate FROM pg_database WHERE datname = current_database()")
        )
        linguistic = list(
            (
                await session.execute(
                    text(
                        "SELECT v FROM unnest(CAST(:values AS text[])) AS v "
                        'ORDER BY v COLLATE "default"'
                    ),
                    {"values": _TIMESTAMPS},
                )
            ).scalars()
        )
    assert collation not in ("C", "POSIX", "C.UTF-8", "C.utf8"), (
        "run against a database whose default collation is linguistic "
        "(e.g. CREATE DATABASE ... LC_COLLATE 'en_US.utf8' TEMPLATE template0)"
    )
    # Guards the test itself: the default collation must misorder this data.
    assert linguistic != sorted(_TIMESTAMPS)

    ids = []
    for index, stamp in enumerate(_TIMESTAMPS):
        row = await isolated_db.create_resume(content=f"r{index}")
        ids.append(row["resume_id"])
        async with isolated_db._write_session() as session:
            await session.execute(
                update(Resume)
                .where(Resume.resume_id == row["resume_id"])
                .values(created_at=stamp, updated_at=stamp)
            )
            await session.commit()

    listed = [row["created_at"] for row in await isolated_db.list_resumes()]
    assert listed == sorted(_TIMESTAMPS)
    async with isolated_db._session() as session:
        collations = set(
            (
                await session.execute(
                    text(
                        "SELECT collation_name FROM information_schema.columns "
                        "WHERE table_schema = current_schema() AND column_name IN "
                        "('created_at', 'updated_at', 'expires_at', "
                        "'claim_expires_at', 'applied_at')"
                    )
                )
            ).scalars()
        )
    assert collations == {"C"}
    pivot = "2026-01-01T00:00:00+00:00"
    async with isolated_db._session() as session:
        later = await session.scalar(
            select(func.count()).select_from(Resume).where(Resume.created_at > pivot)
        )
    assert later == sum(1 for stamp in _TIMESTAMPS if stamp > pivot)


# -- SQLite → PostgreSQL migration -------------------------------------------


async def _populate_sqlite(source: Database) -> dict[str, Any]:
    master = await source.create_resume(
        content="# Zoë Example — 東京",
        is_master=True,
        processing_status="ready",
        processed_data={"personalInfo": {"name": "Zoë Example"}, "summary": "東京"},
        original_markdown="# raw",
    )
    job = await source.create_job("Synthetic job", master["resume_id"])
    await source.update_job(job["job_id"], {"company": "Acme", "job_keywords": ["python"]})
    tailored = await source.create_resume(
        content="Tailored",
        parent_id=master["resume_id"],
        processing_status="ready",
        title="Engineer @ Acme",
    )
    await source.create_improvement(
        master["resume_id"], tailored["resume_id"], job["job_id"], [{"change": "x"}]
    )
    await source.create_application(
        job_id=job["job_id"],
        resume_id=tailored["resume_id"],
        master_resume_id=master["resume_id"],
        company="Acme",
        role="Engineer",
    )
    async with source._write_session() as session:
        session.add(
            TailoringPreview(
                preview_id="preview-1",
                source_id=master["resume_id"],
                job_id=job["job_id"],
                payload_hash="p",
                source_hash="s",
                job_hash="j",
                created_at="2026-01-01T00:00:00+00:00",
                expires_at="2026-01-02T00:00:00.5+00:00",
                improvements=[{"path": "summary"}],
                response_data={"ok": True},
            )
        )
        await session.commit()
    source.set_api_key_ciphertext("openai", crypto.encrypt("sk-synthetic-migration"))
    return {"master": master, "job": job, "tailored": tailored}


async def _snapshot(database: Database) -> dict[str, Any]:
    resumes = await database.list_resumes()
    return {
        "resumes": sorted(resumes, key=lambda row: row["resume_id"]),
        "jobs": sorted(
            [await database.get_job(row["job_id"]) for row in await database.list_applications()],
            key=lambda row: json.dumps(row, sort_keys=True),
        ),
        "applications": await database.list_applications(),
        "improvements": [
            await database.get_improvement_by_tailored_resume(row["resume_id"])
            for row in sorted(resumes, key=lambda row: row["resume_id"])
        ],
        "keys": database.get_api_key_ciphertexts(),
    }


async def test_sqlite_to_postgres_migration_round_trip(
    isolated_db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sqlite_path = tmp_path / "source.db"
    source = Database(db_path=sqlite_path)
    await _populate_sqlite(source)
    expected = await _snapshot(source)
    await source.close()
    assert isolated_db.database_url is not None

    counts = await asyncio.to_thread(
        pg_migration.migrate, sqlite_path, isolated_db.database_url
    )
    assert counts == {
        "api_keys": 1,
        "applications": 1,
        "improvements": 1,
        "jobs": 1,
        "resumes": 2,
        "tailoring_previews": 1,
    }
    assert await _snapshot(isolated_db) == expected
    assert crypto.decrypt(isolated_db.get_api_key_ciphertexts()["openai"]) == (
        "sk-synthetic-migration"
    )
    master = await isolated_db.get_master_resume()
    assert master is not None and master["is_master"] is True

    # A non-empty target is refused, by the function and by the CLI.
    with pytest.raises(pg_migration.TargetNotEmptyError):
        await asyncio.to_thread(pg_migration.migrate, sqlite_path, isolated_db.database_url)
    exit_code = await asyncio.to_thread(
        pg_migration.main,
        ["--sqlite", str(sqlite_path), "--database-url", isolated_db.database_url],
    )
    assert exit_code == 1

    # --force replaces the target's rows inside the same verified transaction.
    await isolated_db.create_job("Row only in the target")
    exit_code = await asyncio.to_thread(
        pg_migration.main,
        ["--sqlite", str(sqlite_path), "--database-url", isolated_db.database_url, "--force"],
    )
    assert exit_code == 0
    assert (await isolated_db.get_stats())["total_jobs"] == 1
    assert await _snapshot(isolated_db) == expected

    # A content mismatch aborts and rolls back everything, including the delete.
    original_hashes = pg_migration._content_hashes

    def tampered(connection: Connection, table: Any) -> dict[str, str]:
        hashes = original_hashes(connection, table)
        if connection.dialect.name == "postgresql" and table.name == "resumes":
            first = next(iter(hashes))
            hashes[first] = "tampered"
        return hashes

    monkeypatch.setattr(pg_migration, "_content_hashes", tampered)
    with pytest.raises(pg_migration.MigrationVerificationError, match="resumes: 1 rows differ"):
        await asyncio.to_thread(
            pg_migration.migrate, sqlite_path, isolated_db.database_url, force=True
        )
    assert await _snapshot(isolated_db) == expected


async def test_migration_writes_absent_json_as_sql_null(
    isolated_db: Database, tmp_path: Path
) -> None:
    sqlite_path = tmp_path / "source.db"
    source = Database(db_path=sqlite_path)
    resume = await source.create_resume(content="Unparsed", processed_data=None)
    async with source._write_session() as session:
        session.add(
            TailoringPreview(
                preview_id="preview-null",
                source_id=resume["resume_id"],
                job_id="job-null",
                payload_hash="p",
                source_hash="s",
                job_hash="j",
                created_at="2026-01-01T00:00:00+00:00",
                expires_at="2026-01-02T00:00:00+00:00",
                improvements=None,
                response_data=None,
            )
        )
        await session.commit()
    await source.close()
    assert isolated_db.database_url is not None

    await asyncio.to_thread(pg_migration.migrate, sqlite_path, isolated_db.database_url)

    async with isolated_db._session() as session:
        for table, column in (
            ("resumes", "processed_data"),
            ("tailoring_previews", "improvements"),
            ("tailoring_previews", "response_data"),
        ):
            assert (
                await session.scalar(
                    text(f"SELECT count(*) FROM {table} WHERE {column} IS NULL")
                )
            ) == 1, f"{table}.{column} was not written as SQL NULL"


async def test_migration_refuses_api_keys_the_data_dir_secret_cannot_read(
    isolated_db: Database, tmp_path: Path
) -> None:
    from cryptography.fernet import Fernet

    sqlite_path = tmp_path / "source.db"
    source = Database(db_path=sqlite_path)
    await source.create_resume(content="Resume")
    foreign = Fernet(Fernet.generate_key()).encrypt(b"sk-other-install").decode()
    source.set_api_key_ciphertext("openai", foreign)
    await source.close()
    crypto.encrypt("x")  # DATA_DIR holds a secret, just not the one used above.
    assert isolated_db.database_url is not None

    with pytest.raises(pg_migration.ApiKeySecretError, match="None of 1 API keys"):
        await asyncio.to_thread(
            pg_migration.migrate, sqlite_path, isolated_db.database_url
        )
    assert await isolated_db.list_resumes() == []  # rolled back
    assert isolated_db.get_api_key_ciphertexts() == {}

    exit_code = await asyncio.to_thread(
        pg_migration.main,
        [
            "--sqlite", str(sqlite_path),
            "--database-url", isolated_db.database_url,
            "--allow-undecryptable-keys",
        ],
    )
    assert exit_code == 0
    assert isolated_db.get_api_key_ciphertexts() == {"openai": foreign}


async def test_migration_refuses_api_keys_without_a_data_dir_secret(
    isolated_db: Database, tmp_path: Path
) -> None:
    from app.config import settings

    sqlite_path = tmp_path / "source.db"
    source = Database(db_path=sqlite_path)
    source.set_api_key_ciphertext("openai", crypto.encrypt("sk-synthetic"))
    await source.close()
    (settings.data_dir / ".secret_key").unlink()
    crypto.reset_cache()
    assert isolated_db.database_url is not None

    with pytest.raises(pg_migration.ApiKeySecretError, match="no secret at"):
        await asyncio.to_thread(
            pg_migration.migrate, sqlite_path, isolated_db.database_url
        )
    assert not (settings.data_dir / ".secret_key").exists()  # never generated
    assert isolated_db.get_api_key_ciphertexts() == {}


async def test_startup_skips_the_tinydb_import_on_postgres(
    isolated_db: Database, caplog: pytest.LogCaptureFixture
) -> None:
    """Counterpart of the sqlite_only real-startup TinyDB migration test."""
    from tinydb import TinyDB

    from app.config import settings

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    legacy = TinyDB(settings.db_path)
    try:
        legacy.table("resumes").insert({"resume_id": "legacy", "content": "x"})
    finally:
        legacy.close()
    with caplog.at_level("WARNING", logger="app.main"):
        async with app.router.lifespan_context(app):
            assert await isolated_db.get_resume("legacy") is None
    assert any(
        record.levelname == "WARNING" and "not imported on PostgreSQL" in record.getMessage()
        for record in caplog.records
    )
    assert settings.db_path.exists()
    assert not settings.db_path.with_suffix(".json.migrated").exists()
