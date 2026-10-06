import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from app.mapping.transform import JsonScalar, Transformation, execute

from morph_bench import manifest
from morph_bench.loader import discover
from morph_bench.models import MappingType

BENCH = manifest.BENCH_DIR


# ---- checksum manifest -------------------------------------------------------------------------


def test_the_committed_manifest_matches_every_fixture() -> None:
    assert manifest.verify() == [], "run `python -m morph_bench.manifest --update` only on purpose"


def test_manifest_covers_every_scenario_key_and_reference() -> None:
    names = set(manifest.read())
    for bundle in discover():
        sid = bundle.scenario.id
        assert f"scenarios/{sid}/scenario.yaml" in names
        assert f"scenarios/{sid}/answer_key.yaml" in names
        assert f"references/{sid}.yaml" in names


@pytest.fixture
def copy_of_bench(tmp_path: Path) -> Path:
    for folder in ("scenarios", "references"):
        shutil.copytree(BENCH / folder, tmp_path / folder)
    return tmp_path


def test_a_changed_answer_key_is_detected(copy_of_bench: Path) -> None:
    manifest_path = copy_of_bench / "scenarios" / "MANIFEST.sha256"
    key = copy_of_bench / "scenarios" / "support_user_to_crm_customer" / "answer_key.yaml"
    key.write_text(key.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    problems = manifest.verify(copy_of_bench, manifest_path)
    assert problems == ["changed: scenarios/support_user_to_crm_customer/answer_key.yaml"]


def test_missing_and_unlisted_files_are_detected(copy_of_bench: Path) -> None:
    manifest_path = copy_of_bench / "scenarios" / "MANIFEST.sha256"
    (copy_of_bench / "references" / "crm_v2_to_support_v2.yaml").unlink()
    extra = copy_of_bench / "scenarios" / "new_one"
    extra.mkdir()
    (extra / "answer_key.yaml").write_text("scenario_id: new_one\n", encoding="utf-8")
    problems = manifest.verify(copy_of_bench, manifest_path)
    assert "missing: references/crm_v2_to_support_v2.yaml" in problems
    assert "not in manifest: scenarios/new_one/answer_key.yaml" in problems


def test_line_endings_do_not_count_as_changes(copy_of_bench: Path) -> None:
    manifest_path = copy_of_bench / "scenarios" / "MANIFEST.sha256"
    key = copy_of_bench / "scenarios" / "crm_v2_to_support_v2" / "answer_key.yaml"
    key.write_bytes(key.read_bytes().replace(b"\n", b"\r\n"))
    assert manifest.verify(copy_of_bench, manifest_path) == []


# ---- reference pipelines -----------------------------------------------------------------------


def _reference(scenario_id: str) -> dict[str, Any]:
    data = yaml.safe_load(
        (BENCH / "references" / f"{scenario_id}.yaml").read_text(encoding="utf-8")
    )
    pipelines: dict[str, Any] = data["pipelines"]
    assert data["scenario_id"] == scenario_id
    return pipelines


def test_every_resolvable_key_entry_has_an_executable_reference_that_matches_its_examples() -> None:
    checked = 0
    for bundle in discover():
        pipelines = _reference(bundle.scenario.id)
        resolvable = {
            m.target_field: m
            for m in bundle.answer_key.mappings
            if m.mapping_type is not MappingType.UNRESOLVED
        }
        assert set(pipelines) == set(resolvable), bundle.scenario.id
        for target, entry in resolvable.items():
            transformation = Transformation.model_validate(pipelines[target])
            assert set(transformation.source_fields) == set(entry.source_fields), (
                bundle.scenario.id,
                target,
            )
            for example in entry.examples:
                record: dict[str, JsonScalar] = dict(example.input)
                result = execute(transformation, record)
                assert result == example.output and type(result) is type(example.output), (
                    bundle.scenario.id,
                    target,
                    example.input,
                    result,
                    example.output,
                )
                checked += 1
    assert checked > 60


def test_unresolved_entries_have_no_reference_pipeline() -> None:
    for bundle in discover():
        unresolved = {
            m.target_field
            for m in bundle.answer_key.mappings
            if m.mapping_type is MappingType.UNRESOLVED
        }
        assert unresolved.isdisjoint(_reference(bundle.scenario.id))
