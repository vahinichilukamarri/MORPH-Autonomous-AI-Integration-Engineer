"""Load and validate scenario folders. Everything loaded here is a read-only fixture."""

import hashlib
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from morph_bench.models import AnswerKey, Bundle, MappingEntry, MappingType, Scenario
from morph_bench.systems import entity_fields, field_validator

SCENARIO_FILE = "scenario.yaml"
ANSWER_KEY_FILE = "answer_key.yaml"
SCENARIOS_ROOT = Path(__file__).resolve().parents[1] / "scenarios"


class ScenarioError(Exception):
    """A scenario folder is invalid. ``problems`` lists every issue found, not just the first."""

    def __init__(self, folder: Path, problems: list[str]) -> None:
        self.folder = folder
        self.problems = problems
        super().__init__(f"{folder.name}: " + "; ".join(problems))


def _read_model[T: BaseModel](path: Path, model: type[T]) -> tuple[T, bytes]:
    folder = path.parent
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ScenarioError(folder, [f"cannot read {path.name}: {exc.strerror}"]) from exc
    try:
        data: Any = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ScenarioError(folder, [f"{path.name} is not valid YAML: {exc}"]) from exc
    try:
        return model.model_validate(data), raw
    except ValidationError as exc:
        problems = [
            f"{path.name}: {'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        ]
        raise ScenarioError(folder, problems) from exc


def load_scenario(folder: Path) -> Scenario:
    return _read_model(folder / SCENARIO_FILE, Scenario)[0]


def load_answer_key(folder: Path) -> AnswerKey:
    return _read_model(folder / ANSWER_KEY_FILE, AnswerKey)[0]


def load_bundle(folder: Path) -> Bundle:
    """Load a scenario and its answer key and check them against the real OpenAPI contracts."""
    scenario, _ = _read_model(folder / SCENARIO_FILE, Scenario)
    key, raw_key = _read_model(folder / ANSWER_KEY_FILE, AnswerKey)
    problems = check_bundle(folder.name, scenario, key)
    if problems:
        raise ScenarioError(folder, problems)
    return Bundle(
        scenario=scenario, answer_key=key, answer_key_sha256=hashlib.sha256(raw_key).hexdigest()
    )


def discover(root: Path = SCENARIOS_ROOT) -> list[Bundle]:
    folders = sorted(p for p in root.iterdir() if (p / SCENARIO_FILE).is_file())
    return [load_bundle(folder) for folder in folders]


def check_bundle(folder_name: str, scenario: Scenario, key: AnswerKey) -> list[str]:
    problems: list[str] = []
    if scenario.id != folder_name:
        problems.append(f"scenario id '{scenario.id}' must equal the folder name '{folder_name}'")
    if key.scenario_id != scenario.id:
        problems.append(f"answer key scenario_id '{key.scenario_id}' != '{scenario.id}'")

    transform = scenario.spec_transform
    source_fields = entity_fields(
        scenario.source.system, scenario.source.entity, scenario.source.contract, transform
    )
    target_fields = entity_fields(
        scenario.target.system, scenario.target.entity, scenario.target.contract, transform
    )
    if source_fields is None:
        problems.append(f"unknown source entity {scenario.source.system}.{scenario.source.entity}")
    if target_fields is None:
        problems.append(f"unknown target entity {scenario.target.system}.{scenario.target.entity}")
    if source_fields is None or target_fields is None:
        return problems

    mapped = {m.target_field for m in key.mappings}
    for name in sorted(mapped - set(target_fields)):
        problems.append(f"target field '{name}' does not exist on {scenario.target.entity}")
    for name in sorted(set(target_fields) - mapped):
        problems.append(f"target field '{name}' has no entry in the answer key")

    for entry in key.mappings:
        problems.extend(_check_entry(scenario, entry, set(source_fields), set(target_fields)))
    return problems


def _check_entry(
    scenario: Scenario, entry: MappingEntry, source_names: set[str], target_names: set[str]
) -> list[str]:
    problems: list[str] = []
    for name in entry.source_fields:
        if name not in source_names:
            problems.append(f"{entry.target_field}: source field '{name}' does not exist")
    if entry.target_field not in target_names or entry.mapping_type is MappingType.UNRESOLVED:
        return problems
    target, source, transform = scenario.target, scenario.source, scenario.spec_transform
    out_check = field_validator(
        target.system, target.entity, entry.target_field, target.contract, transform
    )
    for example in entry.examples:
        for name, value in example.input.items():
            if name in source_names:
                in_check = field_validator(
                    source.system, source.entity, name, source.contract, transform
                )
                if not in_check.is_valid(value):
                    problems.append(
                        f"{entry.target_field}: input {name}={value!r} violates the contract"
                    )
        if not out_check.is_valid(example.output):
            problems.append(
                f"{entry.target_field}: output {example.output!r} violates the target contract"
            )
    return problems
