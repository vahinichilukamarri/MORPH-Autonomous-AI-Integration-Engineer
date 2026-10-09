"""Through the gateway with the real sandbox: generation is gated, and the tests run in a container.

Needs Docker and the sandbox image; run with ``pytest -m docker``. No model, no network.
"""

from pathlib import Path

import pytest
from sqlalchemy import Engine

from app.codegen.sandbox import SandboxRunner
from app.policy.models import Role
from tests.mcp_server.rig import make_rig

pytestmark = pytest.mark.docker


def test_a_generation_is_gated_and_its_tests_run_in_the_real_sandbox(
    test_engine: Engine, tmp_path: Path
) -> None:
    rig = make_rig(test_engine, tmp_path, role=Role.OPERATOR, runner=SandboxRunner())
    world = rig.world()
    generated = rig.call("generate_integration", mapping_run_id=world.mapping_run_id)
    assert generated.status == "ok", generated.body
    result = generated.body["result"]
    assert result["status"] == "READY" and all(result["gate"].values())
    assert generated.body["charged"]["sandbox_run"] == 2  # ruff and mypy in the container
    tested = rig.call(
        "run_generated_tests", integration_id=result["integration_id"], version=result["version"]
    )
    assert tested.status == "ok", tested.body
    outcome = tested.body["result"]
    assert (
        outcome["outcome"] == "OK" and outcome["tests"]["total"] == outcome["tests"]["passed"] > 0
    )
    assert tested.body["charged"]["sandbox_run"] == 1
