"""Load, hash and check policy files. A policy is a versioned artifact, never edited at run time.

* ``load_policy`` reads one YAML file with ``safe_load`` (duplicate keys refused), validates it
  against the strict model and the floor, and hashes the canonical JSON of the parsed model.
* ``policy.lock`` (committed next to the files) names the active version and its hash; the server
  refuses to start on a mismatch. It guards against drift and against edits outside review. It does
  not guard against someone with repository write access, who can change a file and the lock
  together.
* ``versions.json`` records the hash of every released version; a released file never changes, and a
  version that loosens relative to the previous one must declare it.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import ValidationError

from app.policy.canonical import digest
from app.policy.floor import policy_floor_problems
from app.policy.models import PolicyFile, Rule, RuleDecision

POLICY_DIR = Path(__file__).resolve().parents[2] / "policy"
LOCK_NAME = "policy.lock"
VERSIONS_NAME = "versions.json"
FILE_PATTERN = re.compile(r"^morph-policy-(v[0-9]+)\.yaml$")
MAX_POLICY_BYTES = 64 * 1024


class PolicyError(Exception):
    """The policy cannot be used. The server must not start with it."""


@dataclass(frozen=True)
class LoadedPolicy:
    file: PolicyFile
    hash: str
    path: Path

    @property
    def version(self) -> str:
        return self.file.version


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses a mapping with a repeated key (a silent override otherwise)."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=True)
            if key in seen:
                raise PolicyError(f"duplicate key {key!r} in the policy file")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def policy_hash(policy: PolicyFile) -> str:
    return digest(policy.model_dump(mode="json"))


def parse_policy(text: str, *, source: str = "policy") -> PolicyFile:
    if len(text.encode("utf-8")) > MAX_POLICY_BYTES:
        raise PolicyError(f"{source} is larger than {MAX_POLICY_BYTES} bytes")
    try:
        data = yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as error:
        raise PolicyError(f"{source} is not valid YAML: {error}") from error
    if not isinstance(data, dict):
        raise PolicyError(f"{source} must be a mapping at the top level")
    try:
        policy = PolicyFile.model_validate(data)
    except ValidationError as error:
        raise PolicyError(f"{source} is not a valid policy: {error}") from error
    problems = policy_floor_problems(policy)
    if problems:
        raise PolicyError(f"{source} weakens the floor: " + "; ".join(problems))
    return policy


def load_policy(path: Path) -> LoadedPolicy:
    policy = parse_policy(path.read_text(encoding="utf-8"), source=path.name)
    return LoadedPolicy(policy, policy_hash(policy), path)


def policy_path(policy_dir: Path, version: str) -> Path:
    return policy_dir / f"morph-policy-{version}.yaml"


# ---- the lock and the released versions ----------------------------------------------------


def read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PolicyError(f"cannot read {path.name}: {error}") from error
    if not isinstance(data, dict):
        raise PolicyError(f"{path.name} must be a JSON object")
    return data


def load_active(policy_dir: Path = POLICY_DIR) -> LoadedPolicy:
    """The policy named by ``policy.lock``; refuses on any mismatch."""
    lock = read_json(policy_dir / LOCK_NAME)
    version, expected = lock.get("active_version"), lock.get("policy_sha256")
    if not isinstance(version, str) or not isinstance(expected, str):
        raise PolicyError("policy.lock needs active_version and policy_sha256")
    loaded = load_policy(policy_path(policy_dir, version))
    if loaded.hash != expected:
        raise PolicyError(
            f"policy {version} does not match policy.lock (file {loaded.hash[:12]}, "
            f"lock {expected[:12]}): refusing to start"
        )
    return loaded


Change = Literal["neutral", "tightening", "loosening"]


def _key(rule: Rule) -> str:
    return digest(rule.model_dump(mode="json"))


def classify_change(old: PolicyFile, new: PolicyFile) -> Change:
    """Conservative: anything that could permit more than before counts as loosening."""
    old_limits, new_limits = old.session_limits, new.session_limits
    if (
        new_limits.max_model_calls > old_limits.max_model_calls
        or new_limits.max_sandbox_runs > old_limits.max_sandbox_runs
        or new_limits.max_result_bytes > old_limits.max_result_bytes
    ):
        return "loosening"
    old_keys = {_key(r) for r in old.rules}
    new_keys = {_key(r) for r in new.rules}
    added = [r for r in new.rules if _key(r) not in old_keys]
    removed = [r for r in old.rules if _key(r) not in new_keys]
    if any(r.decision is not RuleDecision.DENY for r in added):
        return "loosening"
    if any(r.decision is RuleDecision.DENY for r in removed):
        return "loosening"  # a deny rule that disappeared or changed shape
    if added or removed or new_limits != old_limits:
        return "tightening"
    return "neutral"


def check_policy_dir(policy_dir: Path = POLICY_DIR) -> list[str]:
    """Every problem with the policy directory; empty means the lock and the history are intact."""
    problems: list[str] = []
    try:
        versions = read_json(policy_dir / VERSIONS_NAME)
    except PolicyError as error:
        return [str(error)]
    files = {
        m.group(1): p
        for p in sorted(policy_dir.glob("morph-policy-*.yaml"))
        if (m := FILE_PATTERN.match(p.name))
    }
    for version in sorted(set(files) - set(versions), key=_number):
        problems.append(f"{files[version].name} is not listed in {VERSIONS_NAME}")
    loaded: dict[str, LoadedPolicy] = {}
    for version in sorted(versions, key=_number):
        path = policy_path(policy_dir, version)
        if not path.is_file():
            problems.append(f"released version {version} has no file")
            continue
        try:
            loaded[version] = load_policy(path)
        except PolicyError as error:
            problems.append(str(error))
            continue
        if loaded[version].hash != versions[version]:
            problems.append(f"released version {version} was changed after release")
    ordered = [loaded[v] for v in sorted(loaded, key=_number)]
    for before, after in zip(ordered, ordered[1:], strict=False):
        change = classify_change(before.file, after.file)
        if change == "loosening" and not after.file.loosens:
            problems.append(f"{after.version} loosens {before.version} without declaring it")
    try:
        load_active(policy_dir)
    except PolicyError as error:
        problems.append(str(error))
    return problems


def _number(version: str) -> int:
    return int(version.lstrip("v"))
