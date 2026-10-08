"""The repair-mode smoke test passes condition D's bundle for each approved scenario.

Run by the sandbox job, which has the sandbox image; the default bench run does not collect it:
``uv run pytest smoke_tests -q``. S1 and S3 are also covered by the backend sandbox tests; this adds
S4 and uses the evaluation's own approved mappings for all three.
"""

import tempfile
from pathlib import Path

import pytest
from app.codegen.bundle import write_bundle
from app.codegen.generated_tests import render_tests
from app.codegen.generator import derive_strategy, generate_package
from app.codegen.inputs import load_input
from app.codegen.operations import analyse
from app.codegen.review_gate import decide
from app.repair.smoke import make_smoke_runner, run_smoke
from sqlalchemy.orm import Session

from morph_bench.oracle.build import SAMPLES_DIR, seed_mapping_run
from morph_bench.oracle.models import load_fixture

pytestmark = pytest.mark.docker

SCENARIOS = [
    "crm_customer_to_support_user",
    "support_user_to_crm_customer",
    "crm_v2_to_support_v2",
]


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_the_smoke_test_passes_ds_bundle(session: Session, scenario: str) -> None:
    run_id = seed_mapping_run(session, load_fixture(scenario), with_override=True)
    inp = load_input(session, run_id, SAMPLES_DIR)
    plan = analyse(inp.source, inp.source_entity, inp.target, inp.target_entity)
    decision = decide(inp, plan, allow_partial=False)
    files = {
        **generate_package(inp, plan, decision).files,
        **render_tests(decision.included, inp.samples),
    }
    with tempfile.TemporaryDirectory(prefix="morph-smoke-") as tmp:
        bundle = write_bundle(Path(tmp) / "bundle", files, {})
        result = run_smoke(make_smoke_runner(), bundle, derive_strategy(plan, decision.included))
    assert (result.ok, result.code, result.infra) == (True, "OK", False), result
