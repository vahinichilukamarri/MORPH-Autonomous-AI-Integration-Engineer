"""Throwaway database for tests that need pgvector. The dev database is never touched."""

from collections.abc import Iterator
from pathlib import Path

import app
import pytest
from alembic import command
from alembic.config import Config
from app.settings import get_settings
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

BACKEND_DIR = Path(app.__file__).resolve().parents[1]
SUFFIX = "_bench_test"


def _test_url() -> URL:
    dev = make_url(get_settings().database_url)
    assert dev.database, "DATABASE_URL must name a database"
    return dev.set(database=f"{dev.database}{SUFFIX}")


@pytest.fixture(scope="session")
def test_engine() -> Iterator[Engine]:
    url = _test_url()
    assert url.database is not None and url.database.endswith(SUFFIX)
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

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option(
        "sqlalchemy.url", url.render_as_string(hide_password=False).replace("%", "%%")
    )
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
