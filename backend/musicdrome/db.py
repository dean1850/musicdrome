"""Database engine, session handling and schema bootstrap.

SQLite in WAL mode, mirroring Navidrome's storage choice: a single file that is
trivial to back up and comfortably handles six-figure libraries. All datetimes
are stored as *naive UTC* — SQLite has no timezone type, and mixing aware and
naive values is a reliable source of comparison bugs.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

log = logging.getLogger(__name__)


def utcnow() -> datetime:
    """Current UTC time as a naive datetime — the project-wide convention."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


engine: Engine = create_engine(
    settings.database_url,
    echo=False,
    future=True,
    connect_args={"check_same_thread": False, "timeout": 30},
    pool_pre_ping=True,
)


@event.listens_for(engine, "connect")
def _configure_sqlite(dbapi_connection, _connection_record) -> None:
    """WAL + sane durability/perf pragmas, applied to every connection."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA cache_size=-64000")  # 64 MB page cache
    cursor.execute("PRAGMA temp_store=MEMORY")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for background jobs and CLI work."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _sync_schema() -> None:
    """Add columns and indexes that ``create_all`` cannot.

    ``create_all`` creates missing *tables* but silently leaves an existing one
    alone, so a column added to a model would never reach a database that
    already has the table. There is no migration tool here on purpose — SQLite
    plus a handful of additive ``ALTER TABLE``s keeps an upgrade to a plain
    container pull. Only additions are handled; nothing is ever dropped.
    """
    from sqlalchemy import inspect
    from sqlalchemy.schema import CreateIndex

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if table.name not in tables:
                continue  # create_all just built it, columns and all

            present = {column["name"] for column in inspector.get_columns(table.name)}
            added = False
            for column in table.columns:
                if column.name in present:
                    continue

                type_sql = column.type.compile(engine.dialect)
                clause = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {type_sql}'

                # SQLite cannot add a NOT NULL column without a default, and a
                # callable default (utcnow) has no SQL equivalent — those go in
                # nullable and are filled by the ORM from then on.
                default = getattr(column.default, "arg", None)
                if default is not None and not callable(default):
                    literal = (
                        f"'{default}'"
                        if isinstance(default, str)
                        else str(int(default) if isinstance(default, bool) else default)
                    )
                    clause += f" NOT NULL DEFAULT {literal}"

                connection.exec_driver_sql(clause)
                log.info("added column %s.%s", table.name, column.name)
                added = True

            if not added:
                continue

            existing_indexes = {index["name"] for index in inspector.get_indexes(table.name)}
            for index in table.indexes:
                if index.name not in existing_indexes:
                    connection.execute(CreateIndex(index, if_not_exists=True))


def init_db() -> None:
    """Create any missing tables and indexes."""
    from . import models  # noqa: F401  (registers mappers on Base)

    Base.metadata.create_all(bind=engine)
    _sync_schema()
    log.info("database ready at %s", settings.database_url)
