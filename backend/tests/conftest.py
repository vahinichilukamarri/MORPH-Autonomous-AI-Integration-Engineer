"""Test database: a throwaway ``<dev database>_test`` created, migrated and dropped per run.

Tests never touch the dev database that ingest and the retrieval baseline use.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from alembic import command
from app.settings import get_settings

BACKEND_DIR = Path(__file__).resolve().parents[1]
TEST_DATABASE_SUFFIX = "_test"


def _test_url() -> URL:
    dev = make_url(get_settings().database_url)
    assert dev.database, "DATABASE_URL must name a database"
    return dev.set(database=f"{dev.database}{TEST_DATABASE_SUFFIX}")


@pytest.fixture(scope="session")
def test_engine() -> Iterator[Engine]:
    url = _test_url()
    assert url.database is not None and url.database.endswith(TEST_DATABASE_SUFFIX)
    admin = create_engine(
        url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": 5},
    )
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    except OperationalError as exc:
        pytest.fail(f"Postgres is not reachable ({exc.orig}). Run `./scripts/dev.ps1 up` first.")

    rendered = url.render_as_string(hide_password=False).replace("%", "%%")
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", rendered)
    command.upgrade(config, "head")

    engine = create_engine(url)
    yield engine
    engine.dispose()
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
    admin.dispose()


@pytest.fixture
def session(test_engine: Engine) -> Iterator[Session]:
    """A session whose work (including commits) is rolled back when the test ends."""
    connection = test_engine.connect()
    outer = connection.begin()
    db = Session(connection, join_transaction_mode="create_savepoint")
    try:
        yield db
    finally:
        db.close()
        outer.rollback()
        connection.close()
