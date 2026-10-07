"""The model-assisted codegen conditions.

L1: the model proposes the sync strategy (and a few edge-case source records). The proposal is
parsed, then checked against the discovered contracts; a proposal that does not match them is
rejected (one re-ask through the provider, then a recorded failure, never a silent fallback).
L2: the model writes the sync module itself, behind the static gate and the sandbox.

The model never executes anything and never sees secrets, the oracle, the answer keys or the
mocks' admin interface. Spec text and mapping review data go into delimited untrusted blocks.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.codegen.inputs import CodegenInput, MappedField
from app.codegen.review_gate import GateDecision
from app.discovery.models import Entity, Field, Operation, SystemModel
from app.llm.base import Attempt, BaseLLMProvider, LLMRequest, StructuredResult
from app.mapping.prompts import data_block
from app.mapping.transform import Constant, JsonScalar

PROMPT_VERSION = "codegen-v1"
PROMPT_DIR = Path(__file__).resolve().parent / "prompts" / "v1"
MAX_EDGE_RECORDS = 6
MAX_MODULE_CHARS = 20_000
_PLACEHOLDER = re.compile(r"\{[^{}]+\}")


def _template(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def system_prompt() -> str:
    return _template("system.md").strip()


# ---- response models -------------------------------------------------------------------------


class StrategyProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_mode: Literal["LIST", "KEYS"]
    source_key_field: str
    source_list_path: str | None
    source_get_path: str | None
    source_page_param: str | None
    source_size_param: str | None
    source_items_key: str | None
    source_total_key: str | None
    target_mode: Literal["UPSERT", "CREATE_UPDATE"]
    target_id_field: str
    target_get_path: str
    target_update_method: Literal["PUT", "PATCH"]
    target_update_path: str
    target_create_path: str | None
    target_create_fields: list[str]
    target_update_fields: list[str]
    create_only_fields: list[str]
    omit_if_null_fields: list[str]
    natural_key_field: str | None
    id_assigned_by_target: bool
    target_list_path: str | None
    target_page_param: str | None
    target_size_param: str | None
    target_items_key: str | None
    target_total_key: str | None
    edge_record_json: list[str]
    rationale: str


class SyncModuleProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    notes: str


class ProposalInvalid(ValueError):
    """The proposal does not match the discovered contracts; the message lists every problem."""


# ---- prompt blocks ---------------------------------------------------------------------------


def _operation(op: Operation) -> dict[str, object]:
    return {
        "method": op.method,
        "path": op.path,
        "parameters": [
            {"name": p.name, "in": p.location, "required": p.required} for p in op.parameters
        ],
        "request_entity": op.request_entity,
        "returns": [r.entity for r in op.responses if r.status.startswith("2") and r.entity],
    }


def _field(field: Field) -> dict[str, object]:
    """Structure only: codegen needs names, types and presence rules, not descriptions."""
    meta: dict[str, object] = {
        "name": field.name,
        "type": field.json_type,
        "required": field.required,
        "nullable": field.nullable,
    }
    if field.entity_ref:
        meta["refers_to"] = field.entity_ref
    if field.enum_values:
        meta["allowed_values"] = list(field.enum_values)
    return meta


def _entity(entity: Entity) -> dict[str, object]:
    out: dict[str, object] = {"name": entity.name, "fields": [_field(f) for f in entity.fields]}
    if entity.wrapped_entity:
        out["page_of"] = entity.wrapped_entity
    return out


def _relevant_entities(model: SystemModel, names: set[str]) -> list[Entity]:
    wanted = set(names)
    for op in model.operations:
        for response in op.responses:
            entity = model.entity(response.entity or "")
            if entity is not None and entity.wrapped_entity in wanted:
                wanted.add(entity.name)
        if op.request_entity and any(
            r.entity in wanted for r in op.responses if r.status.startswith("2")
        ):
            wanted.add(op.request_entity)
    return [e for e in model.entities if e.name in wanted]


def _first_op(m: MappedField) -> str | None:
    return m.transformation.steps[0].op if m.transformation else None


def build_blocks(inp: CodegenInput, decision: GateDecision) -> dict[str, str]:
    mapping = [
        {
            "target_field": m.target_field,
            "source_fields": list(m.source_fields),
            "first_step": _first_op(m),
            "is_fixed_value": isinstance(m.transformation and m.transformation.steps[0], Constant),
            "review_status": m.review_status.value,
        }
        for m in decision.included
    ]
    source_ops = [_operation(o) for o in inp.source.operations]
    target_ops = [_operation(o) for o in inp.target.operations]
    return {
        "{{SOURCE_BLOCK}}": data_block(
            "SOURCE_SYSTEM",
            {
                "system": inp.source.name,
                "entity": inp.source_entity,
                "auth": [
                    a.model_dump(mode="json", exclude_none=True) for a in inp.source.auth_schemes
                ],
                "operations": source_ops,
                "entities": [
                    _entity(e) for e in _relevant_entities(inp.source, {inp.source_entity})
                ],
            },
        ),
        "{{TARGET_BLOCK}}": data_block(
            "TARGET_SYSTEM",
            {
                "system": inp.target.name,
                "entity": inp.target_entity,
                "auth": [
                    a.model_dump(mode="json", exclude_none=True) for a in inp.target.auth_schemes
                ],
                "operations": target_ops,
                "entities": [
                    _entity(e) for e in _relevant_entities(inp.target, {inp.target_entity})
                ],
            },
        ),
        "{{MAPPING_BLOCK}}": data_block(
            "COMPILED_FIELD_MAPPINGS",
            {
                "mapped_fields": mapping,
                "target_fields_the_api_cannot_write": list(decision.not_writable),
                "fields_without_an_accepted_mapping": [e.target_field for e in decision.excluded],
            },
        ),
    }


def _render(template: str, blocks: dict[str, str], extra: dict[str, str] | None = None) -> str:
    text = template
    for key, value in {**blocks, **(extra or {})}.items():
        text = text.replace(key, value)
    return text.strip()


def build_l1_request(
    inp: CodegenInput,
    decision: GateDecision,
    *,
    temperature: float = 0.0,
    max_output_tokens: int | None = None,
) -> LLMRequest:
    return LLMRequest(
        system=system_prompt(),
        parts=(_render(_template("l1_user.md"), build_blocks(inp, decision)),),
        schema_name="codegen_strategy",
        temperature=temperature,
        max_output_tokens=max_output_tokens,
    )


def build_l2_request(
    inp: CodegenInput,
    decision: GateDecision,
    *,
    temperature: float = 0.0,
    max_output_tokens: int | None = None,
) -> LLMRequest:
    api = _template("runtime_api.md").strip()
    return LLMRequest(
        system=system_prompt(),
        parts=(
            _render(_template("l2_user.md"), build_blocks(inp, decision), {"{{RUNTIME_API}}": api}),
        ),
        schema_name="codegen_sync_module",
        temperature=temperature,
        max_output_tokens=max_output_tokens,
    )


# ---- L1: validation against the discovered contracts ------------------------------------------


def _norm(path: str) -> str:
    return _PLACEHOLDER.sub("{id}", path)


def _find(model: SystemModel, method: str, path: str) -> Operation | None:
    return next(
        (
            o
            for o in model.operations
            if o.method.upper() == method and _norm(o.path) == _norm(path)
        ),
        None,
    )


def _success_entity(op: Operation) -> str | None:
    return next((r.entity for r in op.responses if r.status.startswith("2") and r.entity), None)


def _check_list(
    model: SystemModel,
    entity: str,
    path: str | None,
    page: str | None,
    size: str | None,
    items: str | None,
    total: str | None,
    label: str,
    problems: list[str],
) -> None:
    op = _find(model, "GET", path or "")
    if op is None or "{" in op.path:
        problems.append(f"{label}: there is no GET list operation at {path!r}")
        return
    wrapper = model.entity(_success_entity(op) or "")
    if wrapper is None or wrapper.wrapped_entity != entity:
        problems.append(f"{label}: {path!r} does not return a page of {entity}")
        return
    query = {p.name for p in op.parameters if p.location == "query"}
    for kind, name in (("page", page), ("size", size)):
        if name not in query:
            problems.append(f"{label}: {name!r} is not a query parameter of {path!r} ({kind})")
    arrays = {f.name for f in wrapper.fields if f.json_type == "array" and f.entity_ref == entity}
    if items not in arrays:
        problems.append(f"{label}: {items!r} is not the array of {entity} items in {wrapper.name}")
    integers = {f.name for f in wrapper.fields if f.json_type == "integer"}
    if total is not None and total not in integers:
        problems.append(f"{label}: {total!r} is not an integer field of {wrapper.name}")


def validate_strategy(
    proposal: StrategyProposal, inp: CodegenInput, included: Sequence[MappedField]
) -> dict[str, Any]:
    """Check a proposal against the contracts and return the strategy dict, or raise."""
    p = proposal
    problems: list[str] = []
    compiled = {m.target_field for m in included}
    src_entity = inp.source.entity(inp.source_entity)
    tgt_entity = inp.target.entity(inp.target_entity)
    if src_entity is None or tgt_entity is None:
        raise ProposalInvalid("the source or target entity does not exist")
    src_fields = {f.name for f in src_entity.fields}
    tgt_fields = {f.name: f for f in tgt_entity.fields}

    if p.source_key_field not in src_fields:
        problems.append(f"source_key_field {p.source_key_field!r} is not a field of the source")
    if p.source_mode == "LIST":
        _check_list(
            inp.source, inp.source_entity, p.source_list_path, p.source_page_param,
            p.source_size_param, p.source_items_key, p.source_total_key, "source", problems,
        )  # fmt: skip
    else:
        op = _find(inp.source, "GET", p.source_get_path or "")
        if op is None or _success_entity(op) != inp.source_entity or "{" not in op.path:
            problems.append(
                f"source: {p.source_get_path!r} is not a GET by id returning {inp.source_entity}"
            )

    getter = _find(inp.target, "GET", p.target_get_path)
    if getter is None or _success_entity(getter) != inp.target_entity or "{" not in getter.path:
        problems.append(f"target: {p.target_get_path!r} is not a GET by id returning the target")
    if p.target_id_field not in tgt_fields:
        problems.append(f"target_id_field {p.target_id_field!r} is not a target field")
    updater = _find(inp.target, p.target_update_method, p.target_update_path)
    if updater is None or "{" not in updater.path:
        problems.append(
            f"target: there is no {p.target_update_method} by id at {p.target_update_path!r}"
        )
    create_request: str | None
    if p.target_mode == "UPSERT":
        if p.target_update_method != "PUT":
            problems.append("an UPSERT target must use PUT")
        create_request = updater.request_entity if updater else None
    else:
        creator = _find(inp.target, "POST", p.target_create_path or "")
        if creator is None or "{" in creator.path:
            problems.append(f"target: there is no POST at {p.target_create_path!r}")
        create_request = creator.request_entity if creator else None
    update_request = updater.request_entity if updater else None
    create_entity = inp.target.entity(create_request or inp.target_entity)
    update_entity = inp.target.entity(update_request or inp.target_entity)
    create_names = {f.name for f in create_entity.fields} if create_entity else set()
    update_names = {f.name for f in update_entity.fields} if update_entity else set()
    for label, fields, allowed in (
        ("target_create_fields", p.target_create_fields, create_names),
        ("target_update_fields", p.target_update_fields, update_names),
    ):
        for name in fields:
            if name not in allowed:
                problems.append(f"{label}: {name!r} is not accepted by that request")
            elif name not in compiled:
                problems.append(f"{label}: {name!r} has no accepted compiled mapping")
    if p.target_mode == "UPSERT":
        if p.create_only_fields:
            problems.append(
                "create_only_fields: not supported with UPSERT (a PUT replaces the record)"
            )
        if set(p.target_update_fields) != set(p.target_create_fields):
            problems.append("UPSERT: target_update_fields must equal target_create_fields")
    for name in p.create_only_fields:
        if name not in p.target_create_fields:
            problems.append(f"create_only_fields: {name!r} is not in target_create_fields")
    for name in p.omit_if_null_fields:
        meta = tgt_fields.get(name)
        if meta is None or meta.required or meta.nullable:
            problems.append(f"omit_if_null_fields: {name!r} is not optional and non-nullable")
    assigned = p.target_id_field not in create_names
    if p.id_assigned_by_target != assigned:
        problems.append(
            f"id_assigned_by_target must be {assigned}: {p.target_id_field!r} "
            f"{'is not' if assigned else 'is'} accepted in the create request"
        )
    if p.natural_key_field is not None:
        if p.natural_key_field not in create_names:
            problems.append(f"natural_key_field {p.natural_key_field!r} is not writable")
        _check_list(
            inp.target, inp.target_entity, p.target_list_path, p.target_page_param,
            p.target_size_param, p.target_items_key, p.target_total_key, "target", problems,
        )  # fmt: skip
    if p.target_mode == "UPSERT" and p.target_id_field not in compiled:
        problems.append("an UPSERT target needs the identity field to be mapped")
    if problems:
        raise ProposalInvalid("; ".join(problems))

    natural = p.natural_key_field
    return {
        "source": {
            "mode": p.source_mode,
            "key_field": p.source_key_field,
            "list_path": p.source_list_path or "",
            "page_param": p.source_page_param or "page",
            "size_param": p.source_size_param or "page_size",
            "page_size": 100,
            "items_key": p.source_items_key or "items",
            "total_key": p.source_total_key,
            "get_path": _norm(p.source_get_path or ""),
        },
        "target": {
            "mode": p.target_mode,
            "id_field": p.target_id_field,
            "get_path": _norm(p.target_get_path),
            "update_method": p.target_update_method,
            "update_path": _norm(p.target_update_path),
            "create_path": p.target_create_path or "",
            "create_fields": tuple(p.target_create_fields),
            "update_fields": tuple(
                f for f in p.target_update_fields if f not in p.create_only_fields
            ),
            "create_only": tuple(p.create_only_fields),
            "omit_if_null": tuple(p.omit_if_null_fields),
            "response_required": tuple(f.name for f in tgt_entity.fields if f.required),
            "natural_key": natural,
            "id_assigned_by_target": p.id_assigned_by_target,
            "list_path": p.target_list_path or "",
            "page_param": p.target_page_param or "page",
            "size_param": p.target_size_param or "page_size",
            "page_size": 100,
            "items_key": p.target_items_key or "items",
            "total_key": p.target_total_key,
        },
    }


def parse_edge_records(
    texts: Sequence[str], inp: CodegenInput
) -> tuple[list[dict[str, JsonScalar]], list[str]]:
    """The proposed edge records that are valid source records, and why the others were dropped."""
    entity = inp.source.entity(inp.source_entity)
    names = {f.name for f in entity.fields} if entity else set()
    kept: list[dict[str, JsonScalar]] = []
    dropped: list[str] = []
    for text in list(texts)[:MAX_EDGE_RECORDS]:
        try:
            value = json.loads(text)
        except ValueError:
            dropped.append("not JSON")
            continue
        if not isinstance(value, dict) or set(value) != names:
            dropped.append("keys are not exactly the source fields")
            continue
        if not all(v is None or isinstance(v, str | int | float | bool) for v in value.values()):
            dropped.append("a value is not a scalar")
            continue
        kept.append(value)
    return kept, dropped


# ---- running the model -----------------------------------------------------------------------


@dataclass(frozen=True)
class StrategyResult:
    strategy: dict[str, Any] | None
    edge_records: tuple[dict[str, JsonScalar], ...]
    dropped_edge_records: tuple[str, ...]
    attempts: tuple[Attempt, ...]
    error: str | None
    prompt_hash: str


def propose_strategy(
    llm: BaseLLMProvider,
    inp: CodegenInput,
    decision: GateDecision,
    *,
    temperature: float = 0.0,
    max_output_tokens: int | None = None,
) -> StrategyResult:
    request = build_l1_request(
        inp, decision, temperature=temperature, max_output_tokens=max_output_tokens
    )
    result: StructuredResult[StrategyProposal] = llm.complete_structured(
        request,
        StrategyProposal,
        validate=lambda proposal: validate_strategy(proposal, inp, decision.included),
    )
    prompt_hash = request.fingerprint(StrategyProposal)
    if result.value is None:
        return StrategyResult(None, (), (), result.attempts, result.final_error, prompt_hash)
    strategy = validate_strategy(result.value, inp, decision.included)
    kept, dropped = parse_edge_records(result.value.edge_record_json, inp)
    return StrategyResult(strategy, tuple(kept), tuple(dropped), result.attempts, None, prompt_hash)


@dataclass(frozen=True)
class ModuleResult:
    source: str | None
    attempts: tuple[Attempt, ...]
    error: str | None
    prompt_hash: str


def propose_sync_module(
    llm: BaseLLMProvider,
    inp: CodegenInput,
    decision: GateDecision,
    *,
    temperature: float = 0.0,
    max_output_tokens: int | None = None,
) -> ModuleResult:
    request = build_l2_request(
        inp, decision, temperature=temperature, max_output_tokens=max_output_tokens
    )

    def check(reply: SyncModuleProposal) -> None:
        if not reply.source.strip():
            raise ValueError("source is empty")
        if len(reply.source) > MAX_MODULE_CHARS:
            raise ValueError(f"source is longer than {MAX_MODULE_CHARS} characters")

    result = llm.complete_structured(request, SyncModuleProposal, validate=check)
    prompt_hash = request.fingerprint(SyncModuleProposal)
    if result.value is None:
        return ModuleResult(None, result.attempts, result.final_error, prompt_hash)
    return ModuleResult(result.value.source, result.attempts, None, prompt_hash)


__all__ = [
    "PROMPT_VERSION",
    "ProposalInvalid",
    "StrategyProposal",
    "SyncModuleProposal",
    "propose_strategy",
    "propose_sync_module",
    "validate_strategy",
]
