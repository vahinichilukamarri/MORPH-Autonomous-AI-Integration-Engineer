"""A run never creates the checkpointer's tables: if they are missing it fails at once.

``ensure_checkpoint_schema`` builds an index CONCURRENTLY, which waits for every open transaction,
so it is called once at startup and a run only checks. This test uses a throwaway database that has
the application's tables (migrations) but no checkpoint tables.
"""

import time
from pathlib import Path

import psycopg
import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session

from alembic import command
from app.repair.service import run_repair
from tests.conftest import BACKEND_DIR
from tests.repair.support import make_rig


def test_a_run_against_a_database_without_the_checkpoint_tables_fails_fast(
    test_engine: Engine, tmp_path: Path
) -> None:
    url = test_engine.url.set(database=f"{test_engine.url.database}_noschema")
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option(
        "sqlalchemy.url", url.render_as_string(hide_password=False).replace("%", "%%")
    )
    command.upgrade(config, "head")
    engine = create_engine(url)
    try:
        with Session(engine) as session:
            rig = make_rig(session, engine, tmp_path, [])
            started = time.monotonic()
            with pytest.raises(psycopg.errors.UndefinedTable):
                run_repair(rig.env)
            assert time.monotonic() - started < 30, "failed fast, did not hang"
            assert rig.live.calls == [], "and no model call was made"
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
        admin.dispose()
