"""Oracle fixtures: held-out records and hand-written expected values."""

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

ORACLE_DIR = Path(__file__).resolve().parents[2] / "oracle"
FIXTURES_DIR = ORACLE_DIR / "fixtures"

Outcome = Literal["CREATED", "UPDATED", "UNCHANGED", "NOT_SYNCABLE", "FAILED"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class OracleRecord(_Frozen):
    name: str
    why: str
    source: dict[str, Any]
    outcome: Outcome
    expect: dict[str, Any] | None = None


class OracleReject(_Frozen):
    name: str
    why: str
    source: dict[str, Any]
    field: str


class ReviewOverride(_Frozen):
    target_field: str
    value: str
    reason: str


class OracleFixture(_Frozen):
    scenario: str
    extends: str | None = None
    source_system: Literal["crm", "support"]
    target_system: Literal["crm", "support"]
    source_key: str
    target_key: str
    contract: Literal["v1", "v2"] = "v1"
    review_override: ReviewOverride | None = None
    initial_target: tuple[dict[str, Any], ...] = ()
    records: tuple[OracleRecord, ...] = ()
    rejects: tuple[OracleReject, ...] = ()
    untouched_after: tuple[str, ...] = ()
    missing_source_key: int | None = None
    reject_target_extra: tuple[dict[str, Any], ...] = ()
    notes: str = Field(default="", exclude=True)

    @property
    def keys_mode(self) -> bool:
        """The source has no list operation: the operator supplies the record ids."""
        return self.source_system == "support"

    def expected_target(self) -> list[dict[str, Any]]:
        """The full target state after the first run: initial records with every expectation
        applied. Keyed by the target key."""
        state = {str(r[self.target_key]): dict(r) for r in self.initial_target}
        for record in self.records:
            if record.expect is not None:
                state[str(record.expect[self.target_key])] = dict(record.expect)
        return list(state.values())


def load_fixture(scenario_id: str) -> OracleFixture:
    raw: dict[str, Any] = yaml.safe_load(
        (FIXTURES_DIR / f"{scenario_id}.yaml").read_text(encoding="utf-8")
    )
    parent_id = raw.get("extends")
    if parent_id:
        parent: dict[str, Any] = yaml.safe_load(
            (FIXTURES_DIR / f"{parent_id}.yaml").read_text(encoding="utf-8")
        )
        for key in ("records", "rejects", "initial_target", "untouched_after"):
            raw.setdefault(key, parent.get(key, []))
        raw.setdefault("review_override", parent.get("review_override"))
    return OracleFixture.model_validate(raw)
