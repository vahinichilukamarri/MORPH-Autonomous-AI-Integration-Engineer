"""Run report: one outcome per record, run-level status and exit code."""

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from morph_runtime.errors import Category

EXIT_OK = 0
EXIT_FATAL = 2
EXIT_PARTIAL = 3


class Outcome(StrEnum):
    CREATED = "CREATED"
    UPDATED = "UPDATED"
    UNCHANGED = "UNCHANGED"
    NOT_SYNCABLE = "NOT_SYNCABLE"
    FAILED = "FAILED"


class RunStatus(StrEnum):
    OK = "OK"
    PARTIAL = "PARTIAL"
    FATAL = "FATAL"


@dataclass(frozen=True)
class RecordResult:
    key: str | None
    outcome: Outcome
    category: Category | None = None
    detail: str = ""
    fields: tuple[str, ...] = ()


@dataclass
class RunReport:
    records: list[RecordResult] = field(default_factory=list)
    fatal_category: Category | None = None
    fatal_detail: str = ""
    requests: dict[str, int] = field(default_factory=dict)

    def add(self, result: RecordResult) -> None:
        self.records.append(result)

    def fail_run(self, category: Category, detail: str) -> None:
        self.fatal_category = category
        self.fatal_detail = detail

    @property
    def status(self) -> RunStatus:
        if self.fatal_category is not None:
            return RunStatus.FATAL
        if any(r.outcome is Outcome.FAILED for r in self.records):
            return RunStatus.PARTIAL
        return RunStatus.OK

    @property
    def exit_code(self) -> int:
        return {RunStatus.OK: EXIT_OK, RunStatus.PARTIAL: EXIT_PARTIAL}.get(self.status, EXIT_FATAL)

    def counts(self) -> dict[str, int]:
        counts = {o.value: 0 for o in Outcome}
        for record in self.records:
            counts[record.outcome.value] += 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "fatal": (
                None
                if self.fatal_category is None
                else {"category": self.fatal_category.value, "detail": self.fatal_detail}
            ),
            "counts": self.counts(),
            "requests": dict(self.requests),
            "records": [
                {
                    "key": r.key,
                    "outcome": r.outcome.value,
                    "category": r.category.value if r.category else None,
                    "detail": r.detail,
                    "fields": list(r.fields),
                }
                for r in self.records
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)
