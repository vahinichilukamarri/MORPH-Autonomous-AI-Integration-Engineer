"""No repair guard trips on condition D's bundle for each of the three approved scenarios.

Backend tests cover S1 and S3 with inline inputs. This one adds S4 and uses the same approved
mappings as the evaluation, so a guard that would reject D itself is found before a real run.
"""

import pytest
from app.codegen.generated_tests import render_tests
from app.codegen.generator import derive_strategy, generate_package
from app.codegen.inputs import load_input
from app.codegen.operations import analyse
from app.codegen.review_gate import decide
from app.repair.guards import GuardInputs, evaluate_guards
from sqlalchemy.orm import Session

from morph_bench.oracle.build import SAMPLES_DIR, seed_mapping_run
from morph_bench.oracle.models import load_fixture

SCENARIOS = [
    "crm_customer_to_support_user",
    "support_user_to_crm_customer",
    "crm_v2_to_support_v2",
]


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_no_guard_trips_on_ds_bundle(session: Session, scenario: str) -> None:
    run_id = seed_mapping_run(session, load_fixture(scenario), with_override=True)
    inp = load_input(session, run_id, SAMPLES_DIR)
    plan = analyse(inp.source, inp.source_entity, inp.target, inp.target_entity)
    decision = decide(inp, plan, allow_partial=False)
    base_tests = render_tests(decision.included, inp.samples)
    files = {**generate_package(inp, plan, decision).files, **base_tests}
    names = {m.target_field for m in decision.included}
    required = {f.name for f in plan.target.create_fields if f.required} & names
    sources = {p: t for p, t in files.items() if p.startswith("integration/") and p.endswith(".py")}
    report = evaluate_guards(
        GuardInputs(
            files=files,
            rebuilt=dict(files),
            owned=frozenset(),
            base_tests=base_tests,
            sources=sources,
            previous_sources=sources,
            strategy=derive_strategy(plan, decision.included),
            required_on_create=required,
            included=names & plan.target.writable,
        )  # fmt: skip
    )
    assert report.findings == (), report.findings
