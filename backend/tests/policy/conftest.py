"""Fixtures for the persistence tests: a private audit chain per test on the throwaway database."""

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import Engine

from app.policy.audit import AuditLog
from app.policy.clock import ManualClock


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def chain_name() -> str:
    return f"test-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def audit(test_engine: Engine, chain_name: str, clock: ManualClock) -> Iterator[AuditLog]:
    """Audit events are committed for real (the log uses its own transactions), so each test gets
    its own chain; the database is dropped when the test run ends."""
    yield AuditLog(test_engine, chain=chain_name, clock=clock)
