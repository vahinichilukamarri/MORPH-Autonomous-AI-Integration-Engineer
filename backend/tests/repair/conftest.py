"""The checkpointer's tables exist before any test opens a transaction."""

import pytest
from sqlalchemy import Engine

from app.repair.service import ensure_checkpoint_schema
from tests.repair.support import conninfo


@pytest.fixture(scope="session", autouse=True)
def checkpoint_schema(test_engine: Engine) -> None:
    ensure_checkpoint_schema(conninfo(test_engine))
