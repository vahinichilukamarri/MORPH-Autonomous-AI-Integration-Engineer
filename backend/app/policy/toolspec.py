"""The 13 tools of the MCP surface: names, effects and strict argument models.

This module holds the *specification* only (no handler, no SDK import), so the REST catalogue, the
policy and the server can all read the same list. The server binds a handler to each name and
refuses to start if the sets differ. A tool whose name matches a forbidden pattern cannot be
specified at all.
"""

import re
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.policy.models import SIDE_EFFECTS, Effect

# Capabilities MORPH never exposes as a tool, whatever the policy says (floor F5).
FORBIDDEN_NAME_PATTERN = re.compile(
    r"(approv|overrid|decide|(set|update|change|edit|modify|reload|replace|delete)_?polic|grad(e|ing)|"
    r"oracle|bench|answer[_-]?key|shell|exec|command|http|fetch|download|read_file|write_file|"
    r"open_file|(get|read|list|dump)_?env|secret|token|attribute|sudo|admin)",
    re.IGNORECASE,
)

MAX_PAGE = 100


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SideEffectArgs(Args):
    """Every side-effect tool accepts the id of an approval granted for this exact call."""

    approval_id: str | None = Field(default=None, pattern=r"^apr_[0-9a-f]{16}$")


class NoArgs(Args):
    pass


class ListSystemsArgs(Args):
    limit: int = Field(default=50, ge=1, le=MAX_PAGE)


class GetSystemArgs(Args):
    system_id: int = Field(ge=1)


class GetMappingRunArgs(Args):
    mapping_run_id: int = Field(ge=1)


class GetIntegrationArgs(Args):
    integration_id: int = Field(ge=1)
    version: int | None = Field(default=None, ge=1)


class GetIntegrationFilesArgs(Args):
    integration_id: int = Field(ge=1)
    version: int = Field(ge=1)


class GetRepairRunArgs(Args):
    run_id: int = Field(ge=1)


class ListAuditEventsArgs(Args):
    limit: int = Field(default=50, ge=1, le=MAX_PAGE)
    after_seq: int | None = Field(default=None, ge=0)


class IngestContractArgs(SideEffectArgs):
    file: str = Field(min_length=1, max_length=300)
    name: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._\- ]*$")


class ProposeMappingArgs(SideEffectArgs):
    source_system_version: int = Field(ge=1)
    target_system_version: int = Field(ge=1)
    source_entity: str = Field(min_length=1, max_length=200)
    target_entity: str = Field(min_length=1, max_length=200)
    # There is deliberately no free-text "requirement" here: the mapping prompt shows that field to
    # the model as a trusted section, and text an agent supplies must never be trusted.


class GenerateIntegrationArgs(SideEffectArgs):
    mapping_run_id: int = Field(ge=1)
    condition: Literal["D", "L1", "L2"] = "D"


class RunGeneratedTestsArgs(SideEffectArgs):
    integration_id: int = Field(ge=1)
    version: int = Field(ge=1)


class RepairIntegrationArgs(SideEffectArgs):
    """Start a fresh-start repair run, or resume one by its run id."""

    mapping_run_id: int | None = Field(default=None, ge=1)
    condition: Literal["L1R", "L2R"] | None = None
    run_id: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _start_or_resume(self) -> "RepairIntegrationArgs":
        starting = self.mapping_run_id is not None and self.condition is not None
        resuming = self.run_id is not None
        if starting == resuming or (resuming and (self.mapping_run_id or self.condition)):
            raise ValueError("give either mapping_run_id with condition, or run_id alone")
        return self


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    effects: frozenset[Effect]
    args_model: type[Args]

    @property
    def side_effect(self) -> bool:
        return bool(SIDE_EFFECTS & self.effects)


def _read(name: str, description: str, model: type[Args]) -> ToolSpec:
    return ToolSpec(name, description, frozenset({Effect.READ}), model)


TOOL_SPECS: tuple[ToolSpec, ...] = (
    _read("list_systems", "List ingested systems (names and ids only).", ListSystemsArgs),
    _read("get_system", "A system's versions, entities and field names, types and descriptions.",
          GetSystemArgs),
    _read("get_mapping_run", "A mapping run and the state of each mapped field.",
          GetMappingRunArgs),
    _read("get_integration", "An integration version: status, gate and sandbox results.",
          GetIntegrationArgs),
    _read("get_integration_files", "The generated files of one version (test data is withheld "
          "unless the system's data is synthetic).", GetIntegrationFilesArgs),
    _read("get_repair_run", "A repair run and what happened at each attempt.", GetRepairRunArgs),
    _read("list_audit_events", "This session's own audit events.", ListAuditEventsArgs),
    _read("describe_policy", "The active policy version, its hash and its rules.", NoArgs),
    ToolSpec("ingest_contract", "Ingest an OpenAPI contract from a file under the spec root.",
             frozenset({Effect.WRITE_LOCAL}), IngestContractArgs),
    ToolSpec("propose_mapping", "Propose a field mapping between two ingested system versions "
             "(calls the configured model).",
             frozenset({Effect.WRITE_LOCAL, Effect.MODEL_CALL}), ProposeMappingArgs),
    ToolSpec("generate_integration", "Generate and gate an integration from a mapping run. "
             "Conditions L1 and L2 call the configured model.",
             frozenset({Effect.WRITE_LOCAL, Effect.EXECUTE_SANDBOX}), GenerateIntegrationArgs),
    ToolSpec("run_generated_tests", "Run the generated tests of a version in the sandbox.",
             frozenset({Effect.EXECUTE_SANDBOX}), RunGeneratedTestsArgs),
    ToolSpec("repair_integration", "Start (or resume) a bounded repair run: at most 4 model "
             "turns, each checked by the gate, the generated tests and a smoke test.",
             frozenset({Effect.WRITE_LOCAL, Effect.MODEL_CALL, Effect.EXECUTE_SANDBOX}),
             RepairIntegrationArgs),
)  # fmt: skip

BY_NAME: dict[str, ToolSpec] = {t.name: t for t in TOOL_SPECS}


def model_involved(tool: str, args: Args) -> bool:
    """Whether this call will use the model provider (a property of the tool and its arguments)."""
    if tool in ("propose_mapping", "repair_integration"):
        return True
    return tool == "generate_integration" and getattr(args, "condition", "D") != "D"


def forbidden_names(names: list[str]) -> list[str]:
    return [n for n in names if FORBIDDEN_NAME_PATTERN.search(n)]
