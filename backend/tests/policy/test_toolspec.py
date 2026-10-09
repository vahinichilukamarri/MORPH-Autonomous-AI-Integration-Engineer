"""The 13 tool specifications: names, effects, strict argument models."""

import pytest
from pydantic import ValidationError

from app.policy.models import SIDE_EFFECTS, Effect
from app.policy.toolspec import (
    BY_NAME,
    TOOL_SPECS,
    GenerateIntegrationArgs,
    GetSystemArgs,
    IngestContractArgs,
    ListSystemsArgs,
    RepairIntegrationArgs,
    model_involved,
)

READ_ONLY = {
    "list_systems", "get_system", "get_mapping_run", "get_integration", "get_integration_files",
    "get_repair_run", "list_audit_events", "describe_policy",
}  # fmt: skip
SIDE_EFFECT = {
    "ingest_contract", "propose_mapping", "generate_integration", "run_generated_tests",
    "repair_integration",
}  # fmt: skip


def test_there_are_thirteen_tools_eight_read_only_and_five_with_side_effects() -> None:
    assert len(TOOL_SPECS) == 13 == len(BY_NAME)
    assert {t.name for t in TOOL_SPECS if not t.side_effect} == READ_ONLY
    assert {t.name for t in TOOL_SPECS if t.side_effect} == SIDE_EFFECT


def test_only_side_effect_tools_accept_an_approval_id() -> None:
    for spec in TOOL_SPECS:
        has = "approval_id" in spec.args_model.model_fields
        assert has == spec.side_effect, spec.name


def test_read_tools_have_only_the_read_effect() -> None:
    for spec in TOOL_SPECS:
        if not spec.side_effect:
            assert spec.effects == {Effect.READ}
        else:
            assert Effect.READ not in spec.effects and spec.effects <= SIDE_EFFECTS


@pytest.mark.parametrize(
    "model, payload",
    [
        (GetSystemArgs, {"system_id": 1, "extra": 1}),
        (GetSystemArgs, {"system_id": "1"}),
        (GetSystemArgs, {"system_id": 0}),
        (GetSystemArgs, {}),
        (ListSystemsArgs, {"limit": 101}),
        (IngestContractArgs, {"file": "a.json"}),
        (IngestContractArgs, {"file": "a.json", "name": "x", "approval_id": "apr_nothex"}),
        (GenerateIntegrationArgs, {"mapping_run_id": 1, "condition": "L3"}),
        (RepairIntegrationArgs, {}),
        (RepairIntegrationArgs, {"mapping_run_id": 1}),
        (RepairIntegrationArgs, {"mapping_run_id": 1, "condition": "L2R", "run_id": 2}),
        (RepairIntegrationArgs, {"run_id": 2, "condition": "L1R"}),
    ],
)
def test_bad_arguments_are_refused(model: type, payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        model(**payload)


def test_good_arguments_are_accepted() -> None:
    assert GetSystemArgs(system_id=3).system_id == 3
    assert IngestContractArgs(file="crm.v1.json", name="crm", approval_id="apr_0123456789abcdef")
    assert RepairIntegrationArgs(mapping_run_id=4, condition="L1R").run_id is None
    assert RepairIntegrationArgs(run_id=9).mapping_run_id is None


def test_the_model_is_involved_only_where_it_should_be() -> None:
    assert model_involved(
        "propose_mapping", BY_NAME["propose_mapping"].args_model.model_construct()
    )
    assert model_involved("repair_integration", RepairIntegrationArgs(run_id=1))
    assert not model_involved("generate_integration", GenerateIntegrationArgs(mapping_run_id=1))
    assert model_involved(
        "generate_integration", GenerateIntegrationArgs(mapping_run_id=1, condition="L2")
    )
    assert not model_involved("run_generated_tests", GetSystemArgs(system_id=1))
