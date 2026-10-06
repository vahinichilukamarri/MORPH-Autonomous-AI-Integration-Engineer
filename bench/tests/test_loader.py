import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from morph_bench.loader import (
    ANSWER_KEY_FILE,
    SCENARIO_FILE,
    SCENARIOS_ROOT,
    ScenarioError,
    discover,
    load_bundle,
)

SCENARIO_ID = "crm_customer_to_support_user"
Data = dict[str, Any]


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    target = tmp_path / SCENARIO_ID
    shutil.copytree(SCENARIOS_ROOT / SCENARIO_ID, target)
    return target


def edit_key(folder: Path, change: Callable[[Data], object]) -> None:
    path = folder / ANSWER_KEY_FILE
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def problems_of(folder: Path) -> list[str]:
    with pytest.raises(ScenarioError) as info:
        load_bundle(folder)
    return info.value.problems


def mapping(data: Data, field: str) -> Data:
    found: Data = next(m for m in data["mappings"] if m["target_field"] == field)
    return found


def test_shipped_scenarios_load() -> None:
    bundles = discover()
    assert [b.scenario.id for b in bundles] == [SCENARIO_ID]
    assert len(bundles[0].answer_key_sha256) == 64


def test_fingerprint_changes_with_the_answer_key(folder: Path) -> None:
    before = load_bundle(folder).answer_key_sha256
    edit_key(folder, lambda d: mapping(d, "tier")["examples"].pop())
    assert load_bundle(folder).answer_key_sha256 != before


def test_missing_target_field_is_reported(folder: Path) -> None:
    edit_key(folder, lambda d: d["mappings"].remove(mapping(d, "tier")))
    assert any("'tier' has no entry" in p for p in problems_of(folder))


def test_unknown_target_field_is_reported(folder: Path) -> None:
    edit_key(folder, lambda d: mapping(d, "tier").update(target_field="serviceTier"))
    problems = problems_of(folder)
    assert any("'serviceTier' does not exist" in p for p in problems)
    assert any("'tier' has no entry" in p for p in problems)


def test_unknown_source_field_is_reported(folder: Path) -> None:
    def change(data: Data) -> None:
        entry = mapping(data, "email_address")
        entry["source_fields"] = ["mail"]
        entry["examples"] = [{"input": {"mail": "a@b.co"}, "output": "a@b.co"}]

    edit_key(folder, change)
    assert any("source field 'mail' does not exist" in p for p in problems_of(folder))


def test_example_output_violating_target_contract_is_reported(folder: Path) -> None:
    edit_key(folder, lambda d: mapping(d, "tier")["examples"][0].update(output="GOLD"))
    assert any("GOLD" in p and "target contract" in p for p in problems_of(folder))


def test_example_input_violating_source_contract_is_reported(folder: Path) -> None:
    edit_key(
        folder,
        lambda d: mapping(d, "userId")["examples"][0]["input"].update(customer_id="1837"),
    )
    assert any("customer_id='1837'" in p for p in problems_of(folder))


def test_integer_output_must_be_integer(folder: Path) -> None:
    edit_key(folder, lambda d: mapping(d, "userId")["examples"][0].update(output="1837"))
    assert any("'1837'" in p for p in problems_of(folder))


def test_id_must_match_folder_name(tmp_path: Path, folder: Path) -> None:
    renamed = tmp_path / "other_name"
    folder.rename(renamed)
    assert any("must equal the folder name" in p for p in problems_of(renamed))


def test_answer_key_scenario_id_must_match(folder: Path) -> None:
    edit_key(folder, lambda d: d.update(scenario_id="something_else"))
    assert any("answer key scenario_id" in p for p in problems_of(folder))


def test_unknown_entity_is_reported(folder: Path) -> None:
    path = folder / SCENARIO_FILE
    text = path.read_text(encoding="utf-8").replace("Customer", "Client")
    path.write_text(text, encoding="utf-8")
    assert any("unknown source entity" in p for p in problems_of(folder))


def test_invalid_yaml_and_missing_files_are_reported(folder: Path) -> None:
    (folder / SCENARIO_FILE).write_text("id: [unclosed", encoding="utf-8")
    assert any("not valid YAML" in p for p in problems_of(folder))
    (folder / SCENARIO_FILE).unlink()
    assert any("cannot read" in p for p in problems_of(folder))


def test_schema_violations_name_the_location(folder: Path) -> None:
    edit_key(folder, lambda d: mapping(d, "tier").update(mapping_type="MAGIC"))
    assert any("mapping_type" in p for p in problems_of(folder))
