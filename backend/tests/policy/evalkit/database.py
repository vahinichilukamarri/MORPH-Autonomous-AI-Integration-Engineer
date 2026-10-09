"""A throwaway migrated database for running the evaluation outside pytest."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url

from alembic import command
from app.settings import get_settings

BACKEND = Path(__file__).resolve().parents[3]
SUFFIX = "_policy_eval"


@contextmanager
def throwaway_engine() -> Iterator[Engine]:
    dev = make_url(get_settings().database_url)
    assert dev.database
    url = dev.set(database=f"{dev.database}{SUFFIX}")
    admin = create_engine(
        url.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        connect_args={"connect_timeout": 5},
    )
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    config = Config(str(BACKEND / "alembic.ini"))
    config.set_main_option(
        "sqlalchemy.url", url.render_as_string(hide_password=False).replace("%", "%%")
    )
    command.upgrade(config, "head")
    engine = create_engine(url)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
        admin.dispose()
