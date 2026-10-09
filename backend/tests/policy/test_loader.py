"""Loading, hashing, the lock, released versions, and rejecting a policy that weakens the floor."""

import json
import shutil
from pathlib import Path

import pytest
import yaml

from app.policy.loader import (
    FILE_PATTERN,
    LOCK_NAME,
    POLICY_DIR,
    VERSIONS_NAME,
    PolicyError,
    check_policy_dir,
    classify_change,
    load_active,
    load_policy,
    parse_policy,
    policy_hash,
)
from app.policy.models import PolicyFile

V1 = (POLICY_DIR / "morph-policy-v1.yaml").read_text(encoding="utf-8")


def copy_dir(tmp_path: Path) -> Path:
    target = tmp_path / "policy"
    shutil.copytree(POLICY_DIR, target)
    return target


def with_rule(text: str, rule: str) -> str:
    return text.rstrip() + "\n" + rule


RULE_ALLOW_PROD = """  - id: P90-allow-prod-tests
    decision: allow
    reason: ALLOWED_BY_RULE
    description: Would let code run against production.
    match:
      tools: [run_generated_tests]
      environments: [production]
"""


def test_the_committed_policy_loads_and_matches_its_lock() -> None:
    active = load_active()
    assert active.version == "v1" and len(active.hash) == 64
    assert check_policy_dir() == []


def test_the_hash_ignores_comments_and_key_order() -> None:
    data = yaml.safe_load(V1)
    shuffled = yaml.safe_dump(data, sort_keys=True)  # keys reordered, comments gone
    assert shuffled != V1
    assert policy_hash(parse_policy(shuffled)) == policy_hash(parse_policy(V1))


def test_a_changed_rule_changes_the_hash() -> None:
    changed = V1.replace("max_model_calls: 12", "max_model_calls: 11")
    assert policy_hash(parse_policy(changed)) != policy_hash(parse_policy(V1))


BAD_FILES = {
    "an-unknown-key": (V1.replace("version: v1", "version: v1\nsurprise: 1"), "valid policy"),
    "a-duplicate-key": (V1.replace("version: v1", "version: v1\nversion: v1"), "duplicate"),
    "a-python-tag": ("!!python/object/apply:os.getcwd []\n", "valid YAML"),
    "a-list-at-the-top": ("- a\n- b\n", "mapping"),
    "an-unknown-tool": (V1.replace("tools: [ingest_contract]", "tools: [no_such]"), "unknown tool"),
    "a-bad-decision": (V1.replace("decision: deny", "decision: maybe"), "valid policy"),
    "a-bad-rule-id": (V1.replace("P01-read-any-role", "read any"), "valid policy"),
    "a-limit-above-the-ceiling": (
        V1.replace("max_sandbox_runs: 40", "max_sandbox_runs: 9999"),
        "F10-limit-ceilings",
    ),
    "code-on-production": (with_rule(V1, RULE_ALLOW_PROD), "F01-exec-target-mock"),
    "too-large": (V1 + "#" * 70_000, "larger than"),
}


@pytest.mark.parametrize("label", sorted(BAD_FILES))
def test_a_bad_or_floor_weakening_file_is_refused(label: str) -> None:
    text, message = BAD_FILES[label]
    with pytest.raises(PolicyError, match=message):
        parse_policy(text)


def test_duplicate_rule_ids_are_refused() -> None:
    duplicated = V1.replace("P02-operator-ingest", "P01-read-any-role")
    with pytest.raises(PolicyError, match="unique"):
        parse_policy(duplicated)


def test_a_lock_mismatch_refuses_to_start(tmp_path: Path) -> None:
    directory = copy_dir(tmp_path)
    path = directory / "morph-policy-v1.yaml"
    path.write_text(V1.replace("max_model_calls: 12", "max_model_calls: 13"), encoding="utf-8")
    with pytest.raises(PolicyError, match="does not match policy.lock"):
        load_active(directory)


def test_a_missing_or_broken_lock_refuses_to_start(tmp_path: Path) -> None:
    directory = copy_dir(tmp_path)
    (directory / LOCK_NAME).write_text("{}", encoding="utf-8")
    with pytest.raises(PolicyError, match="needs active_version"):
        load_active(directory)
    (directory / LOCK_NAME).unlink()
    with pytest.raises(PolicyError, match="cannot read"):
        load_active(directory)


def test_a_released_version_cannot_be_changed(tmp_path: Path) -> None:
    directory = copy_dir(tmp_path)
    path = directory / "morph-policy-v1.yaml"
    path.write_text(V1.replace("max_model_calls: 12", "max_model_calls: 13"), encoding="utf-8")
    problems = check_policy_dir(directory)
    assert any("changed after release" in p for p in problems)


def test_an_unlisted_version_file_is_a_problem(tmp_path: Path) -> None:
    directory = copy_dir(tmp_path)
    (directory / "morph-policy-v2.yaml").write_text(
        V1.replace("version: v1", "version: v2"), "utf-8"
    )
    assert any("not listed" in p for p in check_policy_dir(directory))


def release_v2(directory: Path, text: str) -> list[str]:
    v2 = parse_policy(text)
    (directory / "morph-policy-v2.yaml").write_text(text, encoding="utf-8")
    versions = json.loads((directory / VERSIONS_NAME).read_text("utf-8"))
    versions["v2"] = policy_hash(v2)
    (directory / VERSIONS_NAME).write_text(json.dumps(versions), encoding="utf-8")
    return check_policy_dir(directory)


def test_a_loosening_version_must_declare_it(tmp_path: Path) -> None:
    directory = copy_dir(tmp_path)
    looser = V1.replace("version: v1", "version: v2").replace(
        "max_model_calls: 12", "max_model_calls: 20"
    )
    assert any("loosens v1 without declaring" in p for p in release_v2(directory, looser))


def test_a_declared_loosening_and_a_tightening_pass(tmp_path: Path) -> None:
    declared = V1.replace("version: v1", "version: v2\nloosens: [more model calls for the demo]")
    declared = declared.replace("max_model_calls: 12", "max_model_calls: 20")
    assert release_v2(copy_dir(tmp_path / "a"), declared) == []
    tighter = V1.replace("version: v1", "version: v2").replace(
        "max_model_calls: 12", "max_model_calls: 6"
    )
    assert release_v2(copy_dir(tmp_path / "b"), tighter) == []


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        (lambda t: t.replace("version: v1", "version: v2"), "neutral"),
        (lambda t: t.replace("max_result_bytes: 200000", "max_result_bytes: 100000"), "tightening"),
        (lambda t: t.replace("max_sandbox_runs: 40", "max_sandbox_runs: 41"), "loosening"),
        (
            lambda t: t.replace("decision: needs_approval", "decision: allow"),
            "loosening",
        ),
        (
            lambda t: t.replace("decision: deny", "decision: needs_approval"),
            "loosening",
        ),
    ],
)
def test_changes_are_classified_conservatively(edit: object, expected: str) -> None:
    old = parse_policy(V1)
    new: PolicyFile = parse_policy(edit(V1))  # type: ignore[operator]
    assert classify_change(old, new) == expected


def test_a_removed_allow_rule_is_a_tightening() -> None:
    old = parse_policy(V1)
    start = V1.index("  - id: P04-operator-run-tests")
    end = V1.index("  - id: P05-operator-model-synthetic")
    assert classify_change(old, parse_policy(V1[:start] + V1[end:])) == "tightening"


def test_the_policy_file_names_match_the_file_pattern() -> None:
    assert FILE_PATTERN.match("morph-policy-v1.yaml") and not FILE_PATTERN.match("policy-v1.yaml")
    assert load_policy(POLICY_DIR / "morph-policy-v1.yaml").version == "v1"
