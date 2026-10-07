"""Oracle fixtures: the integration under test (built once per scenario) and ephemeral mocks."""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from app.codegen.sandbox import MockEnvironment, SandboxRunner
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from morph_bench.oracle.build import build_bundle
from morph_bench.oracle.models import load_fixture
from morph_bench.oracle.recorder import write_results
from morph_bench.oracle.world import Harness
from tests.conftest import test_engine  # noqa: F401  (the throwaway database fixture)

SCENARIOS = {
    "S1": "crm_customer_to_support_user",
    "S3": "support_user_to_crm_customer",
    "S4": "crm_v2_to_support_v2",
}

pytestmark = pytest.mark.docker


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "docker: needs Docker, the sandbox and mock images")


@pytest.fixture(scope="session", autouse=True)
def save_results() -> Iterator[None]:
    yield
    write_results()


@pytest.fixture(scope="session")
def sandbox_runner() -> SandboxRunner:
    runner = SandboxRunner()
    runner.verify_limits()
    return runner


@pytest.fixture(scope="module", params=list(SCENARIOS.values()), ids=list(SCENARIOS))
def h(
    request: pytest.FixtureRequest,
    test_engine: Engine,  # noqa: F811
    sandbox_runner: SandboxRunner,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Harness]:
    fixture = load_fixture(request.param)
    external = os.environ.get("MORPH_ORACLE_BUNDLE")
    if external:  # grade a bundle built elsewhere (the evaluation script)
        with MockEnvironment() as mocks:
            yield Harness(fixture, mocks, Path(external))
        return
    with Session(test_engine, expire_on_commit=False) as session:
        version, bundle = build_bundle(
            session, fixture, sandbox_runner, tmp_path_factory.mktemp(fixture.scenario) / "bundle"
        )
        session.commit()
    assert version.status == "READY", (version.status, version.manifest.get("review"))
    with MockEnvironment() as mocks:
        yield Harness(fixture, mocks, Path(bundle))
