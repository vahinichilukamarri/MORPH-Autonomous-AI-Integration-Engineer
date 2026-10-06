from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from app.settings import get_settings


@lru_cache
def get_engine() -> Engine:
    return create_engine(
        get_settings().database_url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 3},
    )


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request; endpoints commit explicitly."""
    with Session(get_engine()) as session:
        yield session
