"""Fixtures: gateways on the throwaway database, as an operator and as a reader."""

from pathlib import Path

import pytest
from sqlalchemy import Engine

from app.policy.models import Role
from tests.mcp_server.rig import Rig, make_rig


@pytest.fixture
def rig(test_engine: Engine, tmp_path: Path) -> Rig:
    return make_rig(test_engine, tmp_path, role=Role.OPERATOR)


@pytest.fixture
def reader(test_engine: Engine, tmp_path: Path) -> Rig:
    return make_rig(test_engine, tmp_path, role=Role.READER)
